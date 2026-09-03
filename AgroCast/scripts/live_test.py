import sys
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

PARTIAL = {("seasonal", 2026, 6), ("monthly", 2026, 8)}


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
    }


def main():
    from agrocast.backtest.engine import run_backtest
    from agrocast.blend.blender import Blender, attach_obs, blended_records
    from agrocast.blend.calibration import TercileCalibrator
    from agrocast.blend.conformal import ConformalQuantileCalibrator
    from agrocast.blend.nn_stack import load_alpha, mix, nn_map, row_keys
    from agrocast.backtest.metrics import clim_rps, rps_rows
    from agrocast.features.dataset import PointDataset
    from agrocast.serve.pipeline import world_config

    wc = world_config("world")
    pt = PointDataset(wc, 43.5, 37.5, wc.zarr_store())
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
        out_dir = BASE / "data" / "audit"
        out_dir.mkdir(parents=True, exist_ok=True)
        rec.to_parquet(out_dir / f"live_records_{mode}.parquet")
        if mode == "seasonal":
            for (name, y, tm), gg in rec[rec.variable == "t2m"].groupby(["model", "year", "target_month"]):
                r = gg.iloc[0]
                print(f"  model={name:14s} {int(y)}-{int(tm):02d} P=({r.p0:.2f},{r.p1:.2f},{r.p2:.2f}) obs={int(r.obs_tercile)} z={r.obs_z:+.2f}")
        else:
            sub = rec[(rec.variable == "t2m") & (rec.lead == 1)]
            for name, gg in sub.groupby("model"):
                gg = gg.sort_values(["year", "target_month"])
                seq = " ".join(f"{int(r.obs_tercile)}" for r in gg.itertuples(index=False))
                print(f"  model={name:14s} obs_seq(lead1, 2025-01..2026-07): {seq}")
            print(f"  obs tercile counts (all leads): {dict(rec[rec.variable == 't2m']['obs_tercile'].value_counts().sort_index())}")
        b = Blender.load(wc.artifact_dir / f"blender_{mode}.json")
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
            cal = TercileCalibrator.load(wc.artifact_dir / f"calib_{mode}_{v}.json")
            if cal is not None and cal.usable():
                P = cal.transform(P)
            cc = ConformalQuantileCalibrator.load(wc.artifact_dir / f"conformal_{mode}_{v}.json")
            if cc is not None and cc.usable():
                Q = np.array([cc.transform(q, v, int(l)) for q, l in zip(Q, sub["lead"].to_numpy(int))])
            m = _metrics(mode, v, sub, P, Q, stds[v])
            m["alpha"] = a
            results[f"{mode}_{v}"] = m
            print(f"{mode:9s} {v}: n={m['n']} years={m['years']} RPSS={m['rpss']:+.3f} hit={m['hit']:.3f} "
                  f"MAE клим={m['mae_clim']} P50={m['mae_p50']} MSE_rel={m['mse_rel']:+.3f} cov={m['coverage']:.3f} alpha={a}")
    hist = {}
    hfile = Path("data/audit/points/BOX_43.50_37.50.parquet")
    if hfile.exists():
        h = pd.read_parquet(hfile)
        for mode, v in (("seasonal", "t2m"), ("seasonal", "tp"), ("monthly", "t2m"), ("monthly", "tp")):
            g = h[(h["mode"] == mode) & (h["variable"] == v)]
            clim_err = g["mu"] - g["fact"]
            p50_err = g["p50"] - g["fact"]
            hist[f"{mode}_{v}"] = {
                "rpss": round(float(1.0 - g["rps"].mean() / g["rps_c"].mean()), 3),
                "hit": round(float(g["hit"].mean()), 3),
                "mae_clim": round(float(clim_err.abs().mean()), 2),
                "mae_p50": round(float(p50_err.abs().mean()), 2),
                "mse_rel": round(float(1.0 - (p50_err ** 2).mean() / (clim_err ** 2).mean()), 3),
            }
        print("история 2004-24 (бокс, путь аудита):")
        for k, m in hist.items():
            print(f"{k:16s} RPSS={m['rpss']:+.3f} hit={m['hit']:.3f} MAE клим={m['mae_clim']} P50={m['mae_p50']} MSE_rel={m['mse_rel']:+.3f}")
    ok = len(hist) == 4
    if not ok:
        print("ГЕЙТ: нет референса BOX — сначала scripts.audit_full points")
    for k, m in results.items():
        if k in hist:
            gap = m["rpss"] - hist[k]["rpss"]
            print(f"ГЕЙТ {k}: 2025-26 {m['rpss']:+.3f} vs 2004-24 {hist[k]['rpss']:+.3f} (разница {gap:+.3f}, порог -0.05)")
            if gap < -0.05:
                ok = False
    print("ПРИЁМКА:", "ПРОВЕРЕН — навык сохранён в 2025-26" if ok else "ПРОВАЛ — разбираться перед публикацией")


if __name__ == "__main__":
    main()
