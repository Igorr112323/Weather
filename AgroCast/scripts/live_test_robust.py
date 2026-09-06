import sys
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

PARTIAL = {("seasonal", 2026, 6), ("monthly", 2026, 8)}
CLAMP_Z = 3.0


class RobustPointDataset:
    def __init__(self, inner):
        self._inner = inner
        ref = inner.predictor_frame()
        ref = ref[ref.index < pd.Period("2024-12", "M")]
        self._mu = ref.mean()
        self._sd = ref.std()

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def predictor_frame(self):
        f = self._inner.predictor_frame()
        num = f.select_dtypes(include=[np.number])
        z = (num - self._mu) / self._sd
        z = z.replace([np.inf, -np.inf], np.nan).clip(-CLAMP_Z, CLAMP_Z).fillna(0.0)
        out = f.copy()
        out[num.columns] = self._mu + z * self._sd
        return out


def _metrics(mode, v, sub, P, Q, std):
    from agrocast.backtest.metrics import clim_rps, rps_rows

    obs = sub["obs_tercile"].to_numpy(int)
    obs_z = sub["obs_z"].to_numpy(float)
    years = sub["year"].to_numpy(int)
    rps = rps_rows(P, obs)
    rps_c = float(clim_rps(obs))
    rpss = 1.0 - float(rps.mean()) / rps_c
    dom = P.argmax(axis=1)
    hit = float((dom == obs).mean())
    t = pd.PeriodIndex([f"{int(y)}-{int(m):02d}" for y, m in zip(years, sub["target_month"].to_numpy(int))], freq="M")
    mu = np.array([float(std.loc[x, "mu"]) for x in t])
    sd = np.array([float(std.loc[x, "sd"]) for x in t])
    fact = mu + sd * obs_z
    clim_err = mu - fact
    p50_err = (mu + sd * Q[:, 1]) - fact
    qlo = mu + sd * Q[:, 0]
    qhi = mu + sd * Q[:, 2]
    cov = float(np.mean((qlo - 1e-9 <= fact) & (fact <= qhi + 1e-9)))
    return {
        "n": int(len(sub)),
        "years": [int(years.min()), int(years.max())],
        "rpss": round(rpss, 3),
        "hit": round(hit, 3),
        "mae_clim": round(float(np.abs(clim_err).mean()), 2),
        "mae_p50": round(float(np.abs(p50_err).mean()), 2),
        "mse_rel": round(float(1.0 - (p50_err ** 2).mean() / (clim_err ** 2).mean()), 3),
        "coverage": round(cov, 3),
        "pred": np.bincount(dom, minlength=3).tolist(),
        "obs_cnt": np.bincount(obs, minlength=3).tolist(),
    }


def main():
    from agrocast.backtest.engine import run_backtest
    from agrocast.blend.blender import Blender, attach_obs, blended_records
    from agrocast.blend.calibration import TercileCalibrator
    from agrocast.blend.conformal import ConformalQuantileCalibrator
    from agrocast.blend.nn_stack import load_alpha, mix, nn_map, row_keys
    from agrocast.features.dataset import PointDataset
    from agrocast.serve.pipeline import world_config

    wc = world_config()
    pt = RobustPointDataset(PointDataset(wc, 43.5, 37.5, wc.zarr_store()))
    results = {}
    for mode, leads in (("seasonal", [1]), ("monthly", list(range(1, 7)))):
        rec = run_backtest(
            wc,
            variables=("t2m", "tp"),
            start_months=list(range(1, 13)),
            leads=leads,
            years=range(2025, 2027),
            mode=mode,
            season_len=3,
            half_life_years=5.0,
            point=pt,
            save_artifacts=False,
        )
        bad = [(mode, int(y), int(m)) in PARTIAL for y, m in zip(rec.year, rec.target_month)]
        rec = rec[~pd.Series(bad, index=rec.index)].copy()
        out_dir = Path(wc.runtime_dir) / "audit"
        out_dir.mkdir(parents=True, exist_ok=True)
        rec.to_parquet(out_dir / f"live_records_robust_{mode}.parquet")
        b = Blender.load(wc.artifact_path(f"blender_{mode}.json"))
        blend = attach_obs(blended_records(rec, b.weights), rec)
        stds = {v: pt.seasonal_std(v, 3) if mode == "seasonal" else pt.standardized(v) for v in ("t2m", "tp")}
        for v in ("t2m", "tp"):
            sub = blend[blend.variable == v]
            if sub.empty:
                continue
            P = sub[["p0", "p1", "p2"]].to_numpy(float)
            Q = sub[["q10", "q50", "q90"]].to_numpy(float)
            a = float(load_alpha(wc, mode, v))
            if a > 0:
                nnmap = nn_map(pt, v, mode, sorted(int(y) for y in sub["year"].unique()))
                P = mix(P, row_keys(sub), nnmap, a)
            cal = TercileCalibrator.load(wc.artifact_path(f"calib_{mode}_{v}.json"))
            if cal is not None and cal.usable():
                P = cal.transform(P)
            cc = ConformalQuantileCalibrator.load(wc.artifact_path(f"conformal_{mode}_{v}.json"))
            if cc is not None and cc.usable():
                Q = np.array([cc.transform(q, v, int(l)) for q, l in zip(Q, sub["lead"].to_numpy(int))])
            m = _metrics(mode, v, sub, P, Q, stds[v])
            m["alpha"] = a
            results[f"{mode}_{v}"] = m
            print(f"ROBUST {mode:9s} {v}: n={m['n']} years={m['years']} RPSS={m['rpss']:+.3f} hit={m['hit']:.3f} "
                  f"MAE клим={m['mae_clim']} P50={m['mae_p50']} MSE_rel={m['mse_rel']:+.3f} cov={m['coverage']:.3f} "
                  f"pred 0/1/2={m['pred']} факт={m['obs_cnt']} alpha={a}")
    print("базовый вариант (без обрезки) для сравнения:")
    print("  seasonal t2m: RPSS=-0.132 hit=0.400 pred 0/1/2=[0,0,15]   факт=[3,6,6]")
    print("  monthly  t2m: RPSS=-0.211 hit=0.247 pred 0/1/2=[0,0,89]   факт=[28,39,22]")


if __name__ == "__main__":
    main()
