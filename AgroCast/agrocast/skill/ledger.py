import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from agrocast.backtest.metrics import ece as ece_metric
from agrocast.backtest.metrics import rps_rows, rpss
from agrocast.forecast.asof import target_end_periods, walk_forward_masks

SEASON_OF = {12: "DJF", 1: "DJF", 2: "DJF", 3: "MAM", 4: "MAM", 5: "MAM",
             6: "JJA", 7: "JJA", 8: "JJA", 9: "SON", 10: "SON", 11: "SON"}


def _ospr_shrink(P, blend, pt, lead=3):
    W = getattr(getattr(pt, "config", None), "ospr_weight", 0.2)
    spr = pt.ocean_spread(lead=lead)
    if spr is None or len(spr) < 20:
        return P
    smin, smax = float(spr.min()), float(spr.max())
    rng = max(smax - smin, 1e-9)
    issues = [
        pd.Period(f"{int(y)}-{int(m):02d}", "M") - int(l)
        for y, m, l in zip(blend["year"], blend["target_month"], blend["lead"])
    ]
    vals = np.array([float(spr.loc[i]) if i in spr.index else np.nan for i in issues])
    u = np.clip((vals - smin) / rng, 0.0, 1.0)
    w = W * u
    w = np.where(np.isfinite(w), w, 0.0)
    P = np.asarray(P, float)
    for i in range(len(P)):
        if w[i] > 0:
            P[i] = (1.0 - w[i]) * P[i] + w[i] * np.array([1.0 / 3.0, 1.0 / 3.0, 1.0 / 3.0])
            P[i] = P[i] / P[i].sum()
    return P


def record_hash(issue, lat, lon, variable, p, q):
    payload = (
        f"{issue}|{float(lat):.4f}|{float(lon):.4f}|{variable}|"
        f"{float(p[0]):.4f},{float(p[1]):.4f},{float(p[2]):.4f}|"
        f"{float(q[0]):.3f},{float(q[1]):.3f},{float(q[2]):.3f}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _block(g, qcov80=None):
    if len(g) == 0:
        return None
    probs = g[["p0", "p1", "p2"]].to_numpy(float)
    obs = g["obs_tercile"].to_numpy(int)
    dom = probs.argmax(axis=1)
    conf = probs.max(axis=1) > 0.45
    out = {
        "n": int(len(g)),
        "rpss": round(float(rpss(probs, obs, months=(g["target_month"].to_numpy(int) if "target_month" in g.columns else None))), 3),
        "hit": round(float((dom == obs).mean()), 3),
        "hit_conf": round(float((dom[conf] == obs[conf]).mean()), 3) if conf.sum() >= 10 else None,
        "coverage_conf": round(float(conf.mean()), 3),
        "ece": ece_metric(probs, obs)["top_label"],
        "ece_classwise": ece_metric(probs, obs)["classwise"],
    }
    return out


def build_ledger(records, mode="monthly", config=None, half_life_years=5.0):


    from agrocast.blend.blender import Blender, blended_records, attach_obs, season_of
    from agrocast.blend.calibration import TercileCalibrator
    from agrocast.blend.conformal import ConformalQuantileCalibrator
    from agrocast.blend.regime_clim import (
        RegimeClimatology,
        SPECS,
        memory_z,
        season_issue_key,
        issue_key_for,
        tercile_grid,
    )

    rec = records.copy()
    if rec.empty:
        return pd.DataFrame()
    from agrocast.blend.nn_stack import load_alpha, mix, nn_map, row_keys

    pf_dict = {}
    mz = {}
    terc = {}
    pt = None
    if config is not None:
        from agrocast.backtest.engine import region_center
        from agrocast.features.dataset import PointDataset

        pt = PointDataset(config, *region_center(config.region), config.zarr_store())
        if SPECS.get(mode):
            pf_dict = {p: row for p, row in pt.predictor_frame().iterrows()}
            mon = pt.monthly()
            for vv, spec in SPECS.get(mode, {}).items():
                for k in spec["mems"]:
                    mz["z" + ("t" if vv == "t2m" else "p") + str(k)] = memory_z(mon, vv, k)
                for vx, k in spec.get("xmems", ()):
                    mz["z" + ("t" if vx == "t2m" else "p") + str(k)] = memory_z(mon, vx, k)
                terc[vv] = tercile_grid(pt, vv)
    pieces_all = []
    fold_span = 3 if mode == "seasonal" else 1
    fold_embargo = fold_span - 1
    rc_cache = {}
    rc_enabled = pt is not None and bool(SPECS.get(mode))
    for v, gv in rec.groupby("variable"):
        g = gv.reset_index(drop=True)
        target_ends = target_end_periods(g, fold_span)
        fold_years = sorted(int(y) for y in g["year"].unique())
        masks = walk_forward_masks(target_ends, fold_years, fold_embargo)
        history = None
        for y in fold_years:
            gy = g[g["year"] == y]
            fit_slice = g[masks[y]]
            b = Blender(half_life_years=half_life_years).fit(fit_slice)
            blend = blended_records(gy, b.weights)
            if blend.empty:
                continue
            blend = attach_obs(blend, g)
            blend["season"] = blend["target_month"].map(season_of)
            fold_until = str(target_ends[masks[y]].max()) if len(fit_slice) else ""
            P = blend[["p0", "p1", "p2"]].to_numpy(float)
            alpha = load_alpha(config, mode, v) if config is not None else 0.0
            if pt is not None and alpha > 0:
                nnmap = nn_map(pt, v, mode, [y])
                P = mix(P, row_keys(blend), nnmap, alpha)
            if history is not None and len(history) >= 120:
                cal = TercileCalibrator().fit(history[["p0", "p1", "p2"]].to_numpy(float), history["obs_tercile"].to_numpy(int))
                if cal.usable():
                    P = cal.transform(P)
            if rc_enabled:
                fold_start = pd.Period(f"{y}-01", "M")
                if y not in rc_cache:
                    rc_cache[y] = RegimeClimatology.fit_history(mode, pt, until_period=fold_start - 1 - fold_embargo)
                rcy = rc_cache[y]
                Pn = []
                for i, r in blend.iterrows():
                    tm, y2, lead = int(r["target_month"]), int(r["year"]), int(r["lead"])
                    if mode == "seasonal":
                        key = season_issue_key(tm, y2)
                        group = r["season"]
                    else:
                        key = issue_key_for(tm, y2, lead)
                        group = f"m{tm}"
                    ip = pd.Period(f"{key // 100}-{key % 100:02d}", "M")
                    prow = pf_dict.get(ip)
                    pprev = pf_dict.get(ip - 3)
                    x = rcy.feature_vector(v, prow, ip, mz, pprev, terc.get(v)) if prow is not None else None
                    Pn.append(rcy.transform(P[i], v, group, y2, x))
                P = np.asarray(Pn)
            if pt is not None and len(P) > 0 and history is not None and len(history) > 0:
                from agrocast.blend.shrink import ShrinkContext, year_skills

                ys = year_skills(
                    history[["p0", "p1", "p2"]].to_numpy(float),
                    history["obs_tercile"].to_numpy(int),
                    history["year"].to_numpy(int),
                )
                ctx = ShrinkContext(mode, pt, v, ys)
                P = np.asarray(
                    [ctx.apply(P[i], int(blend.iloc[i]["target_month"]), int(blend.iloc[i]["year"])) for i in range(len(P))],
                    float,
                )
            if pt is not None and len(P) > 0 and v == "tp" and getattr(config, "ospr_enabled", True):
                P = _ospr_shrink(P, blend, pt)
            blend = blend.assign(p0=P[:, 0], p1=P[:, 1], p2=P[:, 2])
            ccal = ConformalQuantileCalibrator().fit(history) if history is not None and len(history) else ConformalQuantileCalibrator()
            qs = np.array([
                ccal.transform([r["q10"], r["q50"], r["q90"]], v, r["lead"])
                for _, r in blend.iterrows()
            ])
            blend = blend.assign(q10=qs[:, 0], q50=qs[:, 1], q90=qs[:, 2])
            blend = blend.assign(fold_train_until=fold_until)
            if "evaluation" in g.columns:
                blend = blend.assign(evaluation=str(gy["evaluation"].iloc[0]))
            cols = ["variable", "target_month", "lead", "year", "p0", "p1", "p2", "q10", "q50", "q90", "obs_z", "obs_tercile"]
            if "evaluation" in blend.columns:
                cols.append("evaluation")
            history = blend[cols] if history is None else pd.concat([history, blend[cols]], ignore_index=True)
            pieces_all.append(blend)
    led = pd.concat(pieces_all, ignore_index=True)
    probs = led[["p0", "p1", "p2"]].to_numpy(float)
    obs = led["obs_tercile"].to_numpy(int)
    led = led.assign(rps=rps_rows(probs, obs), hit=(probs.argmax(axis=1) == obs))
    led["mode"] = mode
    led["season"] = led["target_month"].map(SEASON_OF)

    def _issue(r):
        tgt = pd.Period(f"{int(r['year'])}-{int(r['target_month']):02d}", "M")

        return str(tgt - int(r["lead"]))

    led["issue"] = [_issue(r) for _, r in led.iterrows()]
    return led.sort_values(["variable", "year", "lead"]).reset_index(drop=True)


def ledger_summary(led):
    if led is None or led.empty:
        return None
    s = {"overall": _block(led)}
    for v, g in led.groupby("variable"):
        s[v] = _block(g)
    by_lead, by_season = {}, {}
    for lk, g in led.groupby(pd.cut(led["lead"], bins=[0, 1, 3, 99], labels=["1", "2-3", "4-6"])):
        b = _block(g)
        if b is not None:
            by_lead[str(lk)] = b
    for se, g in led.groupby("season"):
        b = _block(g)
        if b is not None:
            by_season[str(se)] = b
    s["by_lead"] = by_lead
    s["by_season"] = by_season

    in80 = (led["obs_z"] >= led["q10"] - 1e-9) & (led["obs_z"] <= led["q90"] + 1e-9)
    s["p80_coverage"] = round(float(in80.mean()), 3)
    s["median_abs_median_err"] = round(float(np.median((led["obs_z"] - led["q50"]).abs())), 3)

    last = led.sort_values(["issue", "variable"]).groupby("issue").tail(1).tail(12)
    s["recent"] = [
        {
            "issue": str(r["issue"]),
            "variable": r["variable"],
            "target": f"{r['target_month']:02d}/{r['year']}",
            "lead": int(r["lead"]),
            "p": [round(float(r["p0"]), 2), round(float(r["p1"]), 2), round(float(r["p2"]), 2)],
            "hit": bool(r["hit"]),
        }
        for _, r in last.iterrows()
    ]
    return s


def ledger_path(config, mode):
    return Path(config.artifact_dir) / f"trust_ledger_{mode}.parquet"


def summary_path(config, mode):
    return Path(config.artifact_dir) / f"trust_summary_{mode}.json"


def save_ledger(led, config, mode):
    if led is None or led.empty:
        return None
    led.to_parquet(ledger_path(config, mode))
    s = ledger_summary(led)
    Path(summary_path(config, mode)).write_text(json.dumps(s, ensure_ascii=False, indent=1))
    return s


def load_ledger(config, mode):
    p = config.artifact_path(f"trust_ledger_{mode}.parquet")
    if not p.exists():
        return None, None
    led = pd.read_parquet(p)
    sp = config.artifact_path(f"trust_summary_{mode}.json")
    s = json.loads(sp.read_text()) if sp.exists() else ledger_summary(led)
    return led, s


LIVE_PATH_NAME = "live_ledger.parquet"


def live_path(config):
    return Path(config.artifact_dir) / LIVE_PATH_NAME


def append_live(config, issue, lat, lon, variable, p, q, target=None):

    t = str(target) if target is not None else None
    row = pd.DataFrame(
        [
            {
                "issue": str(issue),
                "lat": float(lat),
                "lon": float(lon),
                "variable": variable,
                "target": t,
                "p0": round(float(p[0]), 4),
                "p1": round(float(p[1]), 4),
                "p2": round(float(p[2]), 4),
                "q10": round(float(q[0]), 3),
                "q50": round(float(q[1]), 3),
                "q90": round(float(q[2]), 3),
                "hash": record_hash(issue, lat, lon, variable, p, q),
            }
        ]
    )
    pth = live_path(config)
    previous = config.artifact_path(LIVE_PATH_NAME)
    if previous.exists():
        old = pd.read_parquet(previous)
        if "target" not in old.columns:
            old["target"] = None
        tmatch = old["target"].isna() if t is None else old["target"].astype(str) == t
        old = old[~((old["issue"] == str(issue)) & (old["variable"] == variable)
                    & (old["lat"] == float(lat)) & (old["lon"] == float(lon)) & tmatch)]
    else:
        old = pd.DataFrame()
    row = pd.concat([old, row], ignore_index=True)
    row.to_parquet(pth)
    return row.iloc[0].to_dict()


def live_summary(config, limit=12):
    pth = config.artifact_path(LIVE_PATH_NAME)
    if not pth.exists():
        return None
    df = pd.read_parquet(pth).sort_values("issue", ascending=False).head(limit)
    return [r.to_dict() for _, r in df.iterrows()]
