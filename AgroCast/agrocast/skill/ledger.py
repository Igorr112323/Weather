"""Реестр доверия (trust ledger) — публичный, защищённый от подделки
счёт навыка системы.

Каждый прогноз (backtest-год или живой выпуск) попадает в реестр строкой:
датой выпуска, координатой, вероятностями, квантилями, фактом и
SHA-256-хэшем входов. Хэш делает запись подлинностно-проверяемой:
если «навык» в отчёте не совпадает с хэшем выпусков — это видно.

Это «банк доверия», которого нет ни у одной публичной агро-сервисной
системы: честный трек-рекорд, пересчитываемый из одних и тех же данных.
"""
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from agrocast.backtest.metrics import rps_rows, rpss

SEASON_OF = {12: "DJF", 1: "DJF", 2: "DJF", 3: "MAM", 4: "MAM", 5: "MAM",
             6: "JJA", 7: "JJA", 8: "JJA", 9: "SON", 10: "SON", 11: "SON"}


def record_hash(issue, lat, lon, variable, p, q):
    payload = (
        f"{issue}|{float(lat):.4f}|{float(lon):.4f}|{variable}|"
        f"{float(p[0]):.4f},{float(p[1]):.4f},{float(p[2]):.4f}|"
        f"{float(q[0]):.3f},{float(q[1]):.3f},{float(q[2]):.3f}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def ece(probs, obs, bins=10):
    """Expected calibration error по максимальной вероятности и её попаданию."""
    probs = np.asarray(probs, float)
    obs = np.asarray(obs, int)
    m = probs.max(axis=1)
    dom = probs.argmax(axis=1)
    hit = (dom == obs).astype(float)
    idx = np.clip((m * bins).astype(int), 0, bins - 1)
    num, den = 0.0, 0.0
    for b in range(bins):
        msk = idx == b
        n = int(msk.sum())
        if n < 5:
            continue
        num += n * abs(float(m[msk].mean()) - float(hit[msk].mean()))
        den += n
    return float(num / den) if den > 0 else None


def _block(g, qcov80=None):
    probs = g[["p0", "p1", "p2"]].to_numpy(float)
    obs = g["obs_tercile"].to_numpy(int)
    dom = probs.argmax(axis=1)
    conf = probs.max(axis=1) > 0.45
    out = {
        "n": int(len(g)),
        "rpss": round(float(rpss(probs, obs)), 3),
        "hit": round(float((dom == obs).mean()), 3),
        "hit_conf": round(float((dom[conf] == obs[conf]).mean()), 3) if conf.sum() >= 10 else None,
        "coverage_conf": round(float(conf.mean()), 3),
        "ece": round(ece(probs, obs), 3) if ece(probs, obs) is not None else None,
    }
    return out


def build_ledger(records, mode="monthly", config=None, half_life_years=5.0):
    """Из записей backtest'а: LOY-бленд + изотоника + конформная калибровка
    → финальные строки с фактами. Протокол без подглядывания: для каждого
    года блендер обучен без него."""
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

    rc = None
    pf_dict = {}
    mz = {}
    terc = {}
    pt = None
    if config is not None:
        from agrocast.backtest.engine import region_center
        from agrocast.features.dataset import PointDataset

        pt = PointDataset(config, *region_center(config.region), config.zarr_store())
        if SPECS.get(mode):
            rc = RegimeClimatology.fit_history(mode, pt)
            pf_dict = {p: row for p, row in pt.predictor_frame().iterrows()}
            mon = pt.monthly()
            for vv, spec in SPECS.get(mode, {}).items():
                for k in spec["mems"]:
                    mz["z" + ("t" if vv == "t2m" else "p") + str(k)] = memory_z(mon, vv, k)
                for vx, k in spec.get("xmems", ()):
                    mz["z" + ("t" if vx == "t2m" else "p") + str(k)] = memory_z(mon, vx, k)
                terc[vv] = tercile_grid(pt, vv)
    pieces_all = []
    for v, gv in rec.groupby("variable"):
        g = gv
        pieces = []
        for y in sorted(g["year"].unique()):
            b = Blender(half_life_years=half_life_years).fit(g[g["year"] != y])
            pieces.append(blended_records(g[g["year"] == y], b.weights))
        blend = pd.concat(pieces, ignore_index=True)
        blend = attach_obs(blend, g)
        blend["season"] = blend["target_month"].map(season_of)
        P = blend[["p0", "p1", "p2"]].to_numpy(float)
        alpha = load_alpha(config, mode, v) if config is not None else 0.0
        if pt is not None and alpha > 0:
            nnmap = nn_map(pt, v, mode, sorted(blend["year"].unique()))
            P = mix(P, row_keys(blend), nnmap, alpha)
        if len(blend) >= 120:
            cal = TercileCalibrator().fit(P, blend["obs_tercile"].to_numpy(int))
            if cal.usable():
                P = cal.transform(P)
        if rc is not None:
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
                x = rc.feature_vector(v, prow, ip, mz, pprev, terc.get(v)) if prow is not None else None
                Pn.append(rc.transform(P[i], v, group, y2, x))
            P = np.asarray(Pn)
        if pt is not None and len(P) > 0:
            from agrocast.blend.shrink import shrink_ledger_block

            P = shrink_ledger_block(
                P,
                blend["obs_tercile"].to_numpy(int),
                blend["year"].to_numpy(int),
                pt,
                v,
                mode,
                blend["target_month"].to_numpy(int),
            )
        blend = blend.assign(p0=P[:, 0], p1=P[:, 1], p2=P[:, 2])
        ccal = ConformalQuantileCalibrator().fit(blend)
        qs = np.array([
            ccal.transform([r["q10"], r["q50"], r["q90"]], v, r["lead"])
            for _, r in blend.iterrows()
        ])
        blend = blend.assign(q10=qs[:, 0], q50=qs[:, 1], q90=qs[:, 2])
        pieces_all.append(blend)
    led = pd.concat(pieces_all, ignore_index=True)
    probs = led[["p0", "p1", "p2"]].to_numpy(float)
    obs = led["obs_tercile"].to_numpy(int)
    led = led.assign(rps=rps_rows(probs, obs), hit=(probs.argmax(axis=1) == obs))
    led["mode"] = mode
    led["season"] = led["target_month"].map(SEASON_OF)

    def _issue(r):
        tgt = pd.Period(f"{int(r['year'])}-{int(r['target_month']):02d}", "M")
        # в движке: issue = start − 1, tgt = start + lead − 1  =>  issue = tgt − lead
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
        by_lead[str(lk)] = _block(g)
    for se, g in led.groupby("season"):
        by_season[str(se)] = _block(g)
    s["by_lead"] = by_lead
    s["by_season"] = by_season
    # покрытие P10–P90 после конформной калибровки (финальные q в реестре)
    in80 = (led["obs_z"] >= led["q10"] - 1e-9) & (led["obs_z"] <= led["q90"] + 1e-9)
    s["p80_coverage"] = round(float(in80.mean()), 3)
    s["median_abs_median_err"] = round(float(np.median((led["obs_z"] - led["q50"]).abs())), 3)
    # последние посчитанные прогнозы (для живой ленты)
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
    p = ledger_path(config, mode)
    if not p.exists():
        return None, None
    led = pd.read_parquet(p)
    sp = summary_path(config, mode)
    s = json.loads(sp.read_text()) if sp.exists() else ledger_summary(led)
    return led, s


LIVE_PATH_NAME = "live_ledger.parquet"


def live_path(config):
    return Path(config.artifact_dir) / LIVE_PATH_NAME


def append_live(config, issue, lat, lon, variable, p, q, target=None):
    """Живой выпуск: строка с хэшем входов (подлинностно-проверяемая)."""
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
    if pth.exists():
        old = pd.read_parquet(pth)
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
    pth = live_path(config)
    if not pth.exists():
        return None
    df = pd.read_parquet(pth).sort_values("issue", ascending=False).head(limit)
    return [r.to_dict() for _, r in df.iterrows()]
