import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from agrocast.backtest.metrics import (block_bootstrap_ci, ece as ece_metric, promotion_decision, rps_rows,
                                      tercile_baseline)

SCHEMA = "grid-skill-v2"
METHOD = "moving-block bootstrap over consecutive target periods; spatial 2x2 cell-block resampling"
BASELINE = "local empirical tercile climatology of each cell over the verified span"
DEFAULT_BLOCK_LEN = {"monthly": 12, "seasonal": 6}
SPATIAL_BLOCK_CELLS = 2


def add_cell_frame(pipe, lat, lon, cell_id):
    out = pipe.copy()
    out["cell_lat"] = float(lat)
    out["cell_lon"] = float(lon)
    out["cell_id"] = str(cell_id)
    return out


def evaluate_combo(pipe, variable, lead):
    g = pipe[(pipe["variable"] == variable) & (pipe["lead"] == int(lead))].copy()
    if g.empty:
        return None
    g["period"] = pd.PeriodIndex(pd.to_datetime(g["target_start"].astype(str) + "-01"), freq="M")
    g = g.sort_values(["period", "cell_lat", "cell_lon"]).reset_index(drop=True)
    P = g[["p0", "p1", "p2"]].to_numpy(float)
    obs = g["obs_tercile"].to_numpy(int)
    rps = rps_rows(P, obs)
    hit = (P.argmax(axis=1) == obs).astype(float)
    phys_lo = (g["mu"] + g["sd"] * g["q10"]).to_numpy(float)
    phys_hi = (g["mu"] + g["sd"] * g["q90"]).to_numpy(float)
    obs_phys = (g["mu"] + g["sd"] * g["obs_z"]).to_numpy(float)
    cover = ((obs_phys >= phys_lo) & (obs_phys <= phys_hi)).astype(float)
    rps_base = np.full(len(g), np.nan)
    for (_clat, _clon), sub in g.groupby(["cell_lat", "cell_lon"]):
        months = np.asarray([p.month for p in sub["period"]])
        base_p = tercile_baseline(sub["obs_tercile"].to_numpy(int), months)
        rps_base[sub.index.to_numpy()] = rps_rows(base_p, sub["obs_tercile"].to_numpy(int))
    if not np.isfinite(rps_base).all():
        rps_base = np.where(np.isfinite(rps_base), rps_base, float(np.nanmean(rps_base)))
    return {"frame": g, "rps": rps, "hit": hit, "coverage": cover, "rps_base": rps_base}


def spatial_block_ids(lats, lons, cell_size=0.5, block_cells=SPATIAL_BLOCK_CELLS):
    lat = np.asarray(lats, float)
    lon = np.asarray(lons, float)
    gi = np.floor((lat - lat.min()) / max(cell_size, 1e-9)).astype(int) // int(block_cells)
    gj = np.floor((lon - lon.min()) / max(cell_size, 1e-9)).astype(int) // int(block_cells)
    return np.char.add(np.char.add(gi.astype(str), ":"), gj.astype(str))


def _spatial_ci(frame, values, n_boot, seed):
    blocks = spatial_block_ids(frame["cell_lat"], frame["cell_lon"])
    uniq = sorted(set(blocks.tolist()))
    values = np.asarray(values, float)
    if len(uniq) <= 1:
        return {"ci95": [None, None], "n_spatial_blocks": len(uniq)}
    means = np.array([values[blocks == u].mean() for u in uniq])
    rng = np.random.default_rng(int(seed) + 17)
    boot = np.array([means[rng.integers(0, len(means), size=len(means))].mean() for _ in range(int(n_boot))])
    lo, hi = np.quantile(boot, [0.025, 0.975])
    return {"ci95": [float(lo), float(hi)], "n_spatial_blocks": int(len(means))}


def _fit_years(artifact_dir, mode, variable):
    years = set()
    source = "none"
    if artifact_dir is None:
        return years, source
    from agrocast.blend.calibration import TercileCalibrator
    from agrocast.blend.conformal import ConformalQuantileCalibrator

    cc = ConformalQuantileCalibrator.load(Path(artifact_dir) / f"conformal_{mode}_{variable}.json")
    if cc is not None and cc.fit_years:
        years.update(int(y) for ys in cc.fit_years.values() for y in ys)
        source = "conformal"
    tc = TercileCalibrator.load(Path(artifact_dir) / f"calib_{mode}_{variable}.json")
    if tc is not None and list(getattr(tc, "fit_years", [])):
        years.update(int(y) for y in tc.fit_years)
        source = "conformal+calib" if source != "none" else "calib"
    return years, source


def summarize(pipe, modes, leads, variables=("t2m", "tp"), n_boot=400, seed=7, block_len=None, artifact_dir=None):
    combos = {}
    for mode in modes:
        for lead in leads[mode]:
            for v in variables:
                ev = evaluate_combo(pipe, v, lead)
                if ev is None:
                    continue
                frame = ev["frame"]
                fit_years, fit_source = _fit_years(artifact_dir, mode, v)
                n_excluded = 0
                if fit_years and "year" in frame.columns:
                    keep = ~frame["year"].isin(sorted(fit_years))
                    n_excluded = int((~keep).sum())
                    if n_excluded < len(frame):
                        for key in ("rps", "hit", "coverage", "rps_base"):
                            ev[key] = ev[key][keep.to_numpy()]
                        frame = frame[keep].reset_index(drop=True)
                rps_v = np.asarray(ev["rps"], float)
                base_v = np.asarray(ev["rps_base"], float)
                mean_rps = float(rps_v.mean())
                mean_base = float(base_v.mean())
                rpss = float(1.0 - mean_rps / mean_base) if mean_base > 0 else None
                rpss_rows_i = 1.0 - rps_v / mean_base if mean_base > 0 else rps_v
                bl = int(DEFAULT_BLOCK_LEN[mode] if block_len is None else block_len)
                stat_t = block_bootstrap_ci(rps_v, bl, n_boot, seed)
                stat_r = block_bootstrap_ci(rpss_rows_i, bl, n_boot, seed)
                stat_hit = block_bootstrap_ci(np.asarray(ev["hit"], float), bl, n_boot, seed)
                stat_cov = block_bootstrap_ci(np.asarray(ev["coverage"], float), bl, n_boot, seed)
                stat_space = _spatial_ci(frame, rps_v, n_boot, seed)
                P = frame[["p0", "p1", "p2"]].to_numpy(float)
                obsv = frame["obs_tercile"].to_numpy(int)
                ece_res = ece_metric(P, obsv)
                width_z = float((frame["q90"] - frame["q10"]).mean()) if {"q90", "q10"} <= set(frame.columns) else None
                evaluation = "replay_revised"
                if "evaluation" in frame.columns and frame["evaluation"].notna().any():
                    evaluation = sorted(set(str(x) for x in frame["evaluation"].dropna()))[0]
                promo = promotion_decision(rpss, stat_r["ci95"])
                combos[f"{mode}_{v}_l{int(lead)}"] = {
                    "mode": mode,
                    "variable": v,
                    "lead": int(lead),
                    "rpss": rpss,
                    "rps": mean_rps,
                    "baseline_rps": mean_base,
                    "baseline": BASELINE,
                    "hit": float(np.asarray(ev["hit"], float).mean()),
                    "coverage80": float(np.asarray(ev["coverage"], float).mean()),
                    "rps_ci95_time": stat_t["ci95"],
                    "rpss_ci95_time": stat_r["ci95"],
                    "hit_ci95_time": stat_hit["ci95"],
                    "coverage_ci95_time": stat_cov["ci95"],
                    "rps_ci95_space": stat_space["ci95"],
                    "skill_promoted": promo["promoted"],
                    "promotion_rule": promo["rule"],
                    "ece_top_label": ece_res["top_label"],
                    "ece_classwise": ece_res["classwise"],
                    "ece_bins": ece_res["n_bins"],
                    "width80_z": width_z,
                    "fit_exclusion": {"source": fit_source, "n_excluded_fit_overlap": n_excluded},
                    "n_rows": int(len(frame)),
                    "n_cells": int(frame["cell_id"].nunique()),
                    "n_periods": int(frame["period"].nunique()),
                    "n_time_blocks": int(stat_t["n_blocks"]),
                    "n_spatial_blocks": int(stat_space["n_spatial_blocks"]),
                    "block_len_months": bl,
                    "method": METHOD,
                    "evaluation": evaluation,
                }
    return combos


def legacy_blocks(combos, years):
    out = {}
    for name, key in (
        ("seasonal_t2m", "seasonal_t2m_l1"),
        ("seasonal_tp", "seasonal_tp_l1"),
        ("monthly_t2m", "monthly_t2m_l1"),
        ("monthly_tp", "monthly_tp_l1"),
    ):
        c = combos.get(key)
        if c is None:
            continue
        out[name] = {
            "rpss": c["rpss"],
            "hit": c["hit"],
            "hit_ci95": c["hit_ci95_time"],
            "conformal_coverage": c["coverage80"],
            "coverage_ci95": c["coverage_ci95_time"],
            "ece": c["ece_top_label"],
            "skill_promoted": c["skill_promoted"],
            "width80_z": c["width80_z"],
            "n": c["n_rows"],
            "years": years,
        }
    return out


def per_point_summary(pipe):
    rows = []
    for (cid, lat, lon), g in pipe.groupby(["cell_id", "cell_lat", "cell_lon"], sort=True):
        row = {"id": str(cid), "lat": float(lat), "lon": float(lon)}
        for mode in sorted(set(g["mode"].tolist())) if "mode" in g.columns else ["monthly"]:
            sub = g[g["mode"] == mode]
            for v in ("t2m", "tp"):
                sv = sub[(sub["variable"] == v) & (sub["lead"] == 1)]
                if sv.empty:
                    continue
                P = sv[["p0", "p1", "p2"]].to_numpy(float)
                obs = sv["obs_tercile"].to_numpy(int)
                row[f"{mode}_{v}_l1_hit"] = float((P.argmax(axis=1) == obs).mean())
                row[f"{mode}_{v}_l1_rps"] = float(rps_rows(P, obs).mean())
        rows.append(row)
    return rows


def build_skill_artifact(combos, by_point, region, region_name, years, n_points, source_note, mode_map=None):
    art = {
        "schema": SCHEMA,
        "source": source_note,
        "region": region,
        "region_name": region_name,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "years": years,
        "n_points": int(n_points),
        "verifications": int(sum(c["n_rows"] for c in combos.values())),
        "combos": combos,
        "by_point": by_point,
        "spatial": {
            "method": METHOD,
            "baseline": BASELINE,
            "block_cells": SPATIAL_BLOCK_CELLS,
            "block_len_months": {m: int(DEFAULT_BLOCK_LEN[m]) for m in sorted(set(c["mode"] for c in combos.values()))},
            "n_spatial_blocks": max((c["n_spatial_blocks"] for c in combos.values()), default=0),
            "n_time_blocks": {k: c["n_time_blocks"] for k, c in combos.items()},
            "evaluation": sorted({c["evaluation"] for c in combos.values()}),
            "ece_definition": "uniform [0,1] bins=10, skip bin rows<5; top-label over dominant class; classwise over classes",
            "promotion_rule": next((c["promotion_rule"] for c in combos.values()), "RPSS > 0 и нижняя граница 95% block-bootstrap CI > 0"),
            "baseline_definition": "empirical tercile climatology per target month within verified span (prior +1)",
        },
    }
    art.update(legacy_blocks(combos, years))
    return art


def write_artifact(path, payload):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    return p
