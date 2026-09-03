import numpy as np
import pandas as pd

from agrocast.core.config import Config
from agrocast.core.geo import region_center
from agrocast.core.mathutils import exp_weights
from agrocast.ingest.registry import Registry
from agrocast.features.dataset import PointDataset, training_data, make_test_row, feature_columns_for
from agrocast.models import build_models
from agrocast.store.zarrstore import ZarrStore
from agrocast.blend.blender import Blender, blended_records
from agrocast.backtest.metrics import rpss


def records_name(mode):
    return f"backtest_records_{mode}.parquet"


def blender_name(mode):
    return f"blender_{mode}.json"


def skill_name(mode):
    return f"skill_map_{mode}.parquet"


def run_backtest(config, variables=("t2m", "tp"), start_months=None, leads=None, years=None, lat=None, lon=None, point=None, mode="monthly", season_len=3, half_life_years=0.0, save_artifacts=True):
    store = config.zarr_store()
    reg = Registry(config.registry_path)
    if point is None:
        lat, lon = (lat, lon) if lat is not None and lon is not None else region_center(config.region)
        point = PointDataset(config, lat, lon, store)
    pf = point.predictor_frame()
    monthly = point.monthly()
    if mode == "seasonal":
        stds = {v: point.seasonal_std(v, season_len) for v in variables}
        leads = [1]
    else:
        stds = {v: point.standardized(v) for v in variables}
    last_p = monthly.index[-1]
    years = list(years) if years is not None else list(range(config.backtest_start, last_p.year + 1))
    start_months = list(start_months) if start_months is not None else list(range(1, 13))
    leads = list(leads) if leads is not None else list(range(1, config.horizon_max + 1))
    rows = []
    for y in years:
        for v in variables:
            std = stds[v]
            if len(std) < 40:
                continue
            for sm in start_months:
                start = pd.Period(f"{y}-{int(sm):02d}", "M")
                issue = start - 1
                if issue not in pf.index:
                    continue
                for lead in leads:
                    tgt = start + (lead - 1)
                    if mode == "seasonal":
                        tgt_end = tgt + (season_len - 1)
                        if tgt_end > last_p:
                            continue
                    else:
                        if tgt > last_p:
                            continue
                    if tgt not in std.index:
                        continue
                    use_cols = feature_columns_for(pf, v, mode=mode)
                    X, yv, meta = training_data(pf, std, v, sm, lead, until=issue, use_cols=use_cols)
                    if len(X) < 18:
                        continue
                    w = exp_weights(len(X), config.clim_half_life)
                    trow = std.loc[tgt]
                    e1, e2 = float(trow["e1"]), float(trow["e2"])
                    x_test = make_test_row(pf, issue, lead, tgt, use_cols=use_cols)
                    models = build_models(config, variable=v, mode=mode)
                    for m in models:
                        try:
                            Xf, yf, metaf, wf = X, yv, meta, w
                            if mode == "seasonal" and getattr(m, "season_months", None) and len(m.season_months) > 1:
                                Xparts, yparts, mparts = [], [], []
                                for s2 in m.season_months:
                                    Xs, ys, ms = training_data(pf, std, v, s2, lead, until=issue, use_cols=use_cols)
                                    if len(Xs):
                                        Xparts.append(Xs)
                                        yparts.append(ys)
                                        mparts.append(ms)
                                if Xparts:
                                    Xf = pd.concat(Xparts, ignore_index=True)
                                    yf = np.concatenate(yparts)
                                    metaf = pd.concat(mparts, ignore_index=True)
                                    wf = exp_weights(len(Xf), config.clim_half_life)
                            m.fit(Xf, yf, w=wf, edges=metaf[["e1", "e2"]].to_numpy(), years=metaf["year"].to_numpy())
                            p, q = m.predict(x_test, e1, e2)
                        except Exception:
                            continue
                        rows.append(
                            {
                                "variable": v,
                                "start_month": int(sm),
                                "lead": int(lead),
                                "target_month": int(tgt.month),
                                "year": int(y),
                                "model": m.name,
                                "p0": float(p[0]),
                                "p1": float(p[1]),
                                "p2": float(p[2]),
                                "q10": float(q[0]),
                                "q50": float(q[1]),
                                "q90": float(q[2]),
                                "obs_z": float(trow["z"]),
                                "obs_tercile": int(0 if trow["z"] < e1 else (1 if trow["z"] <= e2 else 2)),
                                "mu": float(trow["mu"]),
                                "sd": float(trow["sd"]),
                            }
                        )
    records = pd.DataFrame(rows)
    if records.empty:
        reg.log_event("backtest", f"no records mode={mode}")
        return records
    if save_artifacts:
        records.to_parquet(config.artifact_dir / records_name(mode))
        blender = Blender(half_life_years=half_life_years).fit(records)
        blender.save(config.artifact_dir / blender_name(mode))
        br = blended_records(records, blender.weights)
        smap = []
        for (v, tm, ld), g in br.groupby(["variable", "target_month", "lead"]):
            obs = g["obs_tercile"].to_numpy(int)
            probs = g[["p0", "p1", "p2"]].to_numpy(float)
            smap.append({"variable": v, "target_month": int(tm), "lead": int(ld), "rpss": float(rpss(probs, obs)), "n": len(g)})
        pd.DataFrame(smap).to_parquet(config.artifact_dir / skill_name(mode))
        try:
            from agrocast.skill.ledger import build_ledger, save_ledger

            s = save_ledger(build_ledger(records, mode=mode, config=config, half_life_years=half_life_years), config, mode)
            if s:
                reg.log_event("backtest", f"ledger mode={mode} n={s['overall']['n']} rpss={s['overall']['rpss']}")
        except Exception as exc:
            reg.log_event("backtest", f"ledger failed mode={mode}: {exc}")
    reg.log_event("backtest", f"mode={mode} rows={len(records)} years={years[0]}..{years[-1]}")
    return records


def load_records(config, mode="monthly"):
    p = config.artifact_dir / records_name(mode)
    return pd.read_parquet(p) if p.exists() else None


def load_skill_map(config, mode="monthly"):
    p = config.artifact_dir / skill_name(mode)
    return pd.read_parquet(p) if p.exists() else None


def skill_summary(records):
    out = []
    for (v, m), g in records.groupby(["variable", "model"]):
        obs = g["obs_tercile"].to_numpy(int)
        probs = g[["p0", "p1", "p2"]].to_numpy(float)
        out.append({"variable": v, "model": m, "rpss": rpss(probs, obs), "n": len(g)})
    return pd.DataFrame(out)
