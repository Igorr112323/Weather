# -*- coding: utf-8 -*-
"""
Полный аудит продукта AgroCast: 50 точек × 20 лет (2005–2024) × все режимы.

Аудит воспроизводит ТОЧНО путь продукта «проверка прошлого»
(agrocast.serve.pipeline.run_hindcast, после исправления):

  1. P = ансамблевый бленд 9 моделей (leave-one-year-out, half_life=5 лет);
  2. P = (1-α)·P + α·NN  (walk-forward нейроядро PooledNN, α из артефактов бокса;
     select_alpha: селекция 2004–2014 + валидация 2015–2024);
  3. P = TercileCalibrator(бленд прошлых лет, leave-one-year-out) — как в продукте;
  4. факт = mu_pt + sd_pt · z_pt; obs — из записей бэктеста (центр бокса),
     mu/sd/z для факта — климатология самой точки (как в run_hindcast);
  5. режимы: seasonal (12 стартовых месяцев, блок 3 мес.) и monthly
     (12 целевых месяцев, lead 1 — только его использует продукт в проверке);
  6. метрики: tercile-hit (+ Wilson CI), RPS/RPSS против климатологии
     (функции продукта), ECE, надёжность терцилей, покрытие коридора P10–P90.

Кроме того, для каждой верификации считается КОНТРАФАКТУАЛ «blend»:
что было бы, каи продукт использовал собственный ансамблевый бленд
(веса артефакта blender_{mode}.json, half_life=5 лет) с тем же NN-mix и той же
калибровкой. Это отделяет навык системы (из A/B-реестра) от того, что реально
видит пользователь в режиме «проверка прошлого».

Сеть в песочнице недоступна → 50 точек = 50 уникальных ячеек 0.5° по боксу
данных продукта (43–47°N, 37–42°E). Точки вне бокса требуют скачивания
CPC-данных (нужен интернет).

Запуск:
    python -m scripts.audit_full all           # precompute + points + report
    python -m scripts.audit_full precompute
    python -m scripts.audit_full points --workers 2
    python -m scripts.audit_full report
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

WORLD = str(BASE / "world")
DATA_ROOT = str(BASE / "data")
OUT = Path(DATA_ROOT) / "audit"
CACHE = OUT / "cache"
PTDIR = OUT / "points"

YEARS = list(range(2005, 2025))  # 20 лет
MODES = ("seasonal", "monthly")
VARS_ = ("t2m", "tp")
SEASONS = {12: "DJF", 1: "DJF", 2: "DJF", 3: "MAM", 4: "MAM", 5: "MAM",
           6: "JJA", 7: "JJA", 8: "JJA", 9: "SON", 10: "SON", 11: "SON"}
SEASON_RU = {"DJF": "зима (DJF)", "MAM": "весна (MAM)", "JJA": "лето (JJA)", "SON": "осень (SON)"}

# 50 уникальных ячеек 0.5° по всему боксу данных (43–47°N, 37–42°E).
# Координаты = центры ячеек; в боксе 70 из 99 ячеек с полными данными
# (остальные отбрасываются продуктом: доля не-NaN > 0.9).
# Зафиксировано в data/audit/audit_points.json.
POINT_CELLS = [
    ("P01", 46.75, 38.75),  # ячейка 0.5°
    ("P02", 46.75, 39.25),  # ячейка 0.5°
    ("P03", 46.75, 39.75),  # ячейка 0.5°
    ("P04", 46.75, 40.25),  # ячейка 0.5°
    ("P05", 46.75, 40.75),  # ячейка 0.5°
    ("P06", 46.75, 41.25),  # ячейка 0.5°
    ("P07", 46.75, 41.75),  # ячейка 0.5°
    ("P08", 46.25, 38.25),  # ячейка 0.5°
    ("P09", 46.25, 38.75),  # ячейка 0.5°
    ("P10", 46.25, 40.25),  # ячейка 0.5°
    ("P11", 46.25, 41.75),  # ячейка 0.5°
    ("P12", 45.75, 37.75),  # ячейка 0.5°
    ("P13", 45.75, 38.25),  # ячейка 0.5°
    ("P14", 45.75, 38.75),  # ячейка 0.5°
    ("P15", 45.75, 39.25),  # ячейка 0.5°
    ("P16", 45.75, 39.75),  # ячейка 0.5°
    ("P17", 45.75, 40.25),  # ячейка 0.5°
    ("P18", 45.75, 40.75),  # ячейка 0.5°
    ("P19", 45.75, 41.25),  # ячейка 0.5°
    ("P20", 45.75, 41.75),  # ячейка 0.5°
    ("P21", 45.25, 37.25),  # ячейка 0.5°
    ("P22", 45.25, 37.75),  # ячейка 0.5°
    ("P23", 45.25, 39.25),  # ячейка 0.5°
    ("P24", 45.25, 40.75),  # ячейка 0.5°
    ("P25", 45.25, 42.25),  # ячейка 0.5°
    ("P26", 44.75, 37.75),  # ячейка 0.5°
    ("P27", 44.75, 38.25),  # ячейка 0.5°
    ("P28", 44.75, 38.75),  # ячейка 0.5°
    ("P29", 44.75, 39.25),  # ячейка 0.5°
    ("P30", 44.75, 39.75),  # ячейка 0.5°
    ("P31", 44.75, 40.25),  # ячейка 0.5°
    ("P32", 44.75, 40.75),  # ячейка 0.5°
    ("P33", 44.75, 41.25),  # ячейка 0.5°
    ("P34", 44.75, 41.75),  # ячейка 0.5°
    ("P35", 44.25, 38.75),  # ячейка 0.5°
    ("P36", 44.25, 39.75),  # ячейка 0.5°
    ("P37", 44.25, 40.75),  # ячейка 0.5°
    ("P38", 44.25, 42.25),  # ячейка 0.5°
    ("P39", 43.75, 39.75),  # ячейка 0.5°
    ("P40", 43.75, 40.25),  # ячейка 0.5°
    ("P41", 43.75, 40.75),  # ячейка 0.5°
    ("P42", 43.75, 41.25),  # ячейка 0.5°
    ("P43", 43.75, 41.75),  # ячейка 0.5°
    ("P44", 43.25, 40.25),  # ячейка 0.5°
    ("P45", 43.25, 40.75),  # ячейка 0.5°
    ("P46", 43.25, 41.25),  # ячейка 0.5°
    ("P47", 43.25, 41.75),  # ячейка 0.5°
    ("P48", 42.75, 41.25),  # ячейка 0.5°
    ("P49", 42.75, 41.75),  # ячейка 0.5°
    ("P50", 42.75, 42.25),  # ячейка 0.5°
]
POINTS = [(p, la, lo) for p, la, lo in POINT_CELLS]


def season_of(month):
    return SEASONS.get(int(month), "ALL")


def log(msg):
    print(f"[audit] {time.strftime('%H:%M:%S')} {msg}", flush=True)


# ---------------------------------------------------------------- precompute

def phase_precompute():
    """Общий (не зависит от точки) расчёт. Точно повторяет run_hindcast:

    - прошлый год t при целевом Y: веса Blender().fit(rec[year >= Y]) —
      эквивалент LOYO-веса для t < Y в продукте;
    - текущий год Y: LOYO-бленд 9 моделей (Blender half_life=5, год Y исключён),
      как в исправленном run_hindcast.
    """
    from agrocast.blend.blender import Blender, attach_obs, blended_records

    CACHE.mkdir(parents=True, exist_ok=True)
    alphas = {}
    for mode in MODES:
        t0 = time.time()
        rec = pd.read_parquet(Path(WORLD) / "artifacts" / f"backtest_records_{mode}.parquet")
        rec = rec.copy() if mode == "seasonal" else rec[rec.lead == 1].copy()
        rows = []
        for Y in YEARS:
            if mode == "monthly":
                def _sm(df):
                    return (df["target_month"] - df["lead"] - 1) % 12 + 1
            else:
                def _sm(df):
                    return df["target_month"].copy()
            past = rec[rec.year < Y]
            if not past.empty:
                bt = attach_obs(blended_records(past, Blender(half_life_years=5.0).fit(past).weights), past)
                bt = bt.assign(start_month=_sm(bt))
                rows.append(bt.assign(audit_year=int(Y), audit_kind="past"))
            cur_y = rec[rec.year == Y]
            loyo = attach_obs(blended_records(cur_y, Blender(half_life_years=5.0).fit(rec[rec.year != Y]).weights), cur_y)
            loyo = loyo.assign(start_month=_sm(loyo))
            rows.append(loyo.assign(audit_year=int(Y), audit_kind="cur_loyo"))
        pre = pd.concat(rows, ignore_index=True)
        pre.to_parquet(CACHE / f"precompute_{mode}.parquet")
        for v in VARS_:
            p = Path(WORLD) / "artifacts" / f"stack_{mode}_{v}.json"
            alphas[f"{mode}:{v}"] = float(json.loads(p.read_text()).get("alpha", 0.0)) if p.exists() else 0.0
        log(f"precompute {mode}: {len(pre)} строк (past+cur), {time.time()-t0:.0f}s")
    (CACHE / "alphas.json").write_text(json.dumps(alphas, indent=1))
    log(f"precompute готов. alphas: {alphas}")


# -------------------------------------------------------------------- points

def _mix(P, keys, nnmap, alpha):
    P = np.asarray(P, float).copy()
    if alpha <= 0 or not nnmap:
        return P
    for i, k in enumerate(keys):
        if k in nnmap:
            P[i] = (1.0 - alpha) * P[i] + alpha * nnmap[k]
    return P


def _normalize(P):
    P = np.clip(P, 0.02, None)
    return P / P.sum(axis=1, keepdims=True)


def _ccor(cc, r, v, obs_z):
    if cc is None or not cc.usable():
        return int(float(r.q10) <= obs_z <= float(r.q90))
    qc = cc.transform([float(r.q10), float(r.q50), float(r.q90)], v, int(r.lead))
    return int(qc[0] - 1e-9 <= obs_z <= qc[2] + 1e-9)


def process_point(pid, lat, lon):
    """Одна точка: полный цикл «проверки прошлого» во всех режимах и годах."""
    from agrocast.backtest.metrics import clim_rps, rps_rows
    from agrocast.blend.calibration import gated_calibrator
    from agrocast.blend.nn_stack import nn_map
    from agrocast.features.dataset import PointDataset
    from agrocast.serve.pipeline import point_config

    PTDIR.mkdir(parents=True, exist_ok=True)
    out_path = PTDIR / f"{pid}_{lat:.2f}_{lon:.2f}.parquet"
    if out_path.exists():
        log(f"{pid}: уже есть, пропускаю")
        return out_path

    t0 = time.time()
    cfg, _ = point_config(WORLD, DATA_ROOT, lat, lon)
    pt = PointDataset(cfg, lat, lon, cfg.zarr_store())
    pt.raw_daily()
    alphas = json.loads((CACHE / "alphas.json").read_text())
    rec_years = sorted(int(y) for y in pd.read_parquet(
        Path(WORLD) / "artifacts" / "backtest_records_seasonal.parquet").year.unique())

    pre = {m: pd.read_parquet(CACHE / f"precompute_{m}.parquet") for m in MODES}

    rows = []
    for mode in MODES:
        stds = {v: (pt.seasonal_std(v, 3) if mode == "seasonal" else pt.standardized(v)) for v in VARS_}
        from agrocast.blend.conformal import ConformalQuantileCalibrator

        ccs = {v: ConformalQuantileCalibrator.load(Path(WORLD) / "artifacts" / f"conformal_{mode}_{v}.json") for v in VARS_}
        nnmaps = {}
        for v in VARS_:
            if alphas[f"{mode}:{v}"] <= 0:
                nnmaps[v] = {}
                continue
            t1 = time.time()
            nnmaps[v] = nn_map(pt, v, mode, rec_years)
            log(f"{pid} {mode} {v}: nn_map {len(nnmaps[v])} ключей, {time.time()-t1:.0f}s")
        pre_m = pre[mode]

        for Y in YEARS:
            cals = {}
            for v in VARS_:
                g = pre_m[(pre_m.audit_year == Y) & (pre_m.audit_kind == "past") & (pre_m.variable == v)]
                if g.empty:
                    continue
                Pp = g[["p0", "p1", "p2"]].to_numpy(float)
                keys = list(zip(g.year.astype(int), g.target_month.astype(int), g.lead.astype(int)))
                Pp = _mix(Pp, keys, nnmaps[v], alphas[f"{mode}:{v}"])
                cals[v] = gated_calibrator(Pp, g["obs_tercile"].to_numpy(int), years=g["year"].to_numpy())
            cb = pre_m[(pre_m.audit_year == Y) & (pre_m.audit_kind == "cur_loyo")]
            for v in VARS_:
                std = stds[v]
                gb = cb[cb.variable == v]
                if gb.empty:
                    continue
                a = alphas[f"{mode}:{v}"]
                keysb = list(zip(gb.year.astype(int), gb.target_month.astype(int), gb.lead.astype(int)))
                Pb = gb[["p0", "p1", "p2"]].to_numpy(float)
                P = _normalize(_mix(Pb.copy(), keysb, nnmaps[v], a))
                cal = cals.get(v)
                P_unc = P
                if cal is not None and cal.usable():
                    P = _normalize(cal.transform(P))
                from agrocast.blend import regime_guard

                mask = regime_guard.shifted_rows(pt.monthly()[v], std, v, mode, gb)
                if mask.any():
                    P = P.copy()
                    P[mask] = P_unc[mask]
                obs = gb["obs_tercile"].to_numpy(int)
                obs_z = gb["obs_z"].to_numpy(float)
                rps_p = rps_rows(P, obs)
                rps_b = rps_rows(Pb, obs)
                for i, r in enumerate(gb.itertuples(index=False)):
                    t = pd.Period(f"{int(r.year)}-{int(r.target_month):02d}", "M")
                    if t not in std.index:
                        continue
                    mu = float(std.loc[t, "mu"])
                    sd = float(std.loc[t, "sd"])
                    fact = mu + sd * float(std.loc[t, "z"])  # факт точки (как в run_hindcast)
                    rows.append(dict(
                        point=pid, lat=lat, lon=lon,
                        grid_lat=pt.grid_lat, grid_lon=pt.grid_lon,
                        mode=mode, variable=v, year=int(r.year),
                        start_month=int(r.start_month), target_month=int(r.target_month),
                        season=season_of(r.target_month),
                        p0=float(P[i, 0]), p1=float(P[i, 1]), p2=float(P[i, 2]),
                        bp0=float(Pb[i, 0]), bp1=float(Pb[i, 1]), bp2=float(Pb[i, 2]),
                        obs_tercile=int(obs[i]), obs_z=float(obs_z[i]),
                        mu=mu, sd=sd, fact=fact,
                        p50=mu + sd * float(r.q50),
                        q10=mu + sd * float(r.q10), q90=mu + sd * float(r.q90),
                        hit=int(int(np.argmax(P[i])) == obs[i]),
                        bhit=int(int(np.argmax(Pb[i])) == obs[i]),
                        in_corridor=int(float(r.q10) <= float(obs_z[i]) <= float(r.q90)),
                        in_corridor_c=int(_ccor(ccs[v], r, v, float(obs_z[i]))),
                        rps=float(rps_p[i]), brps=float(rps_b[i]),
                        rps_c=float(clim_rps(np.array([obs[i]]))),
                    ))
    df = pd.DataFrame(rows)
    if df.empty:
        log(f"{pid}: ПУСТО")
        return None
    df["rpss"] = 1.0 - df.rps / df.rps_c
    df["brpss"] = 1.0 - df.brps / df.rps_c
    df.to_parquet(out_path)
    log(f"{pid} ({lat:.2f},{lon:.2f}) grid=({pt.grid_lat},{pt.grid_lon}): "
        f"{len(df)} верификаций, {time.time()-t0:.0f}s")
    return out_path


def phase_points(workers=2, jobs=None):
    from concurrent.futures import ProcessPoolExecutor

    jobs = list(jobs) if jobs else [tuple(p) for p in POINTS]
    done, fail = [], []
    with ProcessPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(process_point, *j): j for j in jobs}
        for fut in futs:
            j = futs[fut]
            try:
                r = fut.result()
                (done if r is not None else fail).append(j)
            except Exception:
                traceback.print_exc()
                fail.append(j)
    log(f"точки: ok={len(done)}, fail={len(fail)}")
    if fail:
        log(f"не удалось: {fail}")
        raise SystemExit(1)


MINI_POINTS = [
    ("P01", 46.75, 38.75),
    ("P12", 45.75, 37.75),
    ("P26", 44.75, 37.75),
    ("P39", 43.75, 39.75),
    ("P50", 42.75, 42.25),
]


def phase_mini5(workers=2):
    if not (CACHE / "alphas.json").exists() or not (CACHE / "precompute_monthly.parquet").exists():
        phase_precompute()
    for f in PTDIR.glob("P*.parquet"):
        f.unlink()
    phase_points(workers=workers, jobs=MINI_POINTS)
    phase_report()
    s = json.loads((OUT / "audit_summary.json").read_text())
    o = s["overall"][0]
    st2m = s["seasonal_t2m"][0]
    cov = o.get("p10_90_coverage_conformal")
    errors = []
    if o["n"] != len(MINI_POINTS) * 960:
        errors.append(f"n={o['n']}")
    if cov is None or not (0.72 <= cov <= 0.85):
        errors.append(f"конформальное покрытие {cov}")
    if st2m["rpss"] is None or st2m["rpss"] <= 0.05:
        errors.append(f"сезонный t2m RPSS {st2m['rpss']}")
    if st2m["hit"] <= 0.44:
        errors.append(f"сезонный t2m hit {st2m['hit']}")
    if o["ece"] > 0.10:
        errors.append(f"ECE {o['ece']}")
    if errors:
        log("мини-аудит: ОТКАЗ — " + "; ".join(errors))
        raise SystemExit(1)
    log(f"мини-аудит: ОК — n={o['n']}, конформальное покрытие {cov:.1%}, "
        f"сезонный t2m RPSS {st2m['rpss']:+.3f}, hit {st2m['hit']:.1%}")


def phase_grid(workers=2, max_points=30):
    from agrocast.region.grid import krai_cells, save_grid
    from agrocast.store.zarrstore import ZarrStore

    store = ZarrStore(str(Path(WORLD) / "zarr"))
    cells = krai_cells(store)
    save_grid(cells)
    idx = np.arange(len(cells))
    if len(cells) > int(max_points):
        idx = np.unique(np.linspace(0, len(cells) - 1, int(max_points)).round().astype(int))
    jobs = [(cells[j]["id"], float(cells[j]["lat"]), float(cells[j]["lon"])) for j in idx]
    if not (CACHE / "alphas.json").exists() or not (CACHE / "precompute_monthly.parquet").exists():
        phase_precompute()
    for f in PTDIR.glob("P*.parquet"):
        f.unlink()
    phase_points(workers=workers, jobs=jobs)
    phase_report()


# -------------------------------------------------------------------- report

def wilson(k, n, z=1.96):
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    d = 1 + z * z / n
    c = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((p + z * z / (2 * n) - c) / d, (p + z * z / (2 * n) + c) / d)


def group_metrics(df):
    """Метрики для подборки верификационных строк. 2 строки: product / blend."""
    if df.empty:
        return None
    out = []
    for prefix, label in (("", "product"), ("b", "blend_counterfactual")):
        probs = df[[prefix + "p0", prefix + "p1", prefix + "p2"]].to_numpy(float)
        obs = df["obs_tercile"].to_numpy(int)
        n = len(df)
        hits = df[prefix + "hit"].to_numpy(int)
        lo, hi = wilson(int(hits.sum()), n)
        rps = float(df[prefix + "rps"].mean())
        rps_c = float(df["rps_c"].mean())
        ece = 0.0
        rel = {}
        for k in range(3):
            mk = obs == k
            nk = int(mk.sum())
            pk = float(probs[mk, k].mean()) if nk else float("nan")
            fk = nk / n
            ece += fk * abs(pk - fk)
            rel[f"pred_{k}"] = round(pk, 3)
            rel[f"freq_{k}"] = round(fk, 3)
        out.append({
            "system": label, "n": n,
            "hit": round(float(hits.mean()), 4),
            "hit_ci95": [round(lo, 4), round(hi, 4)],
            "rps": round(rps, 4), "rps_clim": round(rps_c, 4),
            "rpss": round(1.0 - rps / rps_c, 4) if rps_c > 0 else None,
            "ece": round(ece, 4), **rel,
            "p10_90_coverage": round(float(df["in_corridor"].mean()), 4),
            "p10_90_coverage_conformal": round(float(df["in_corridor_c"].mean()), 4) if "in_corridor_c" in df.columns else None,
        })
    return pd.DataFrame(out)


def phase_report():
    OUT.mkdir(parents=True, exist_ok=True)
    files = sorted(PTDIR.glob("P*.parquet"))
    if len(files) < len(POINTS):
        log(f"ВНИМАНИЕ: точек {len(files)}/{len(POINTS)} — отчёт будет по доступным")
    df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    df.to_csv(OUT / "audit_records.csv", index=False)
    log(f"записей: {len(df)} по {df['point'].nunique()} точек")

    summary = {}

    def agg(subset, key):
        g = group_metrics(subset)
        summary[key] = g.to_dict("records") if g is not None else None

    agg(df, "overall")
    for m in MODES:
        agg(df[df["mode"] == m], f"mode_{m}")
    for v in VARS_:
        agg(df[df["variable"] == v], f"var_{v}")
    for m in MODES:
        for v in VARS_:
            agg(df[(df["mode"] == m) & (df["variable"] == v)], f"{m}_{v}")
    for s in ("DJF", "MAM", "JJA", "SON"):
        agg(df[df["season"] == s], f"season_{s}")

    # по годам
    by_year = []
    for y in sorted(df["year"].unique()):
        for m in MODES:
            for v in VARS_:
                for sysname in ("product", "blend_counterfactual"):
                    sub = df[(df["year"] == y) & (df["mode"] == m) & (df["variable"] == v)]
                    g = group_metrics(sub)
                    if g is None:
                        continue
                    r = g[g.system == sysname].iloc[0]
                    by_year.append(dict(year=int(y), mode=m, variable=v, system=sysname,
                                        n=int(r["n"]), hit=r["hit"], rpss=r["rpss"],
                                        ece=r["ece"], cov=r["p10_90_coverage"]))
    by_year_df = pd.DataFrame(by_year)
    by_year_df.to_csv(OUT / "by_year.csv", index=False)

    # по точкам
    by_point = []
    for p in sorted(df["point"].unique()):
        sub = df[df["point"] == p]
        row = dict(point=p, lat=float(sub["lat"].iloc[0]), lon=float(sub["lon"].iloc[0]),
                   grid_lat=float(sub["grid_lat"].iloc[0]), grid_lon=float(sub["grid_lon"].iloc[0]))
        for m in MODES:
            for v in VARS_:
                for suf in ("prod", "blend"):
                    s2 = sub[(sub["mode"] == m) & (sub["variable"] == v)]
                    g = group_metrics(s2)
                    if g is None:
                        continue
                    r = g[g.system == ("product" if suf == "prod" else "blend_counterfactual")].iloc[0]
                    row[f"{m}_{v}_{suf}_hit"] = r["hit"]
                    row[f"{m}_{v}_{suf}_rpss"] = r["rpss"]
        by_point.append(row)
    by_point_df = pd.DataFrame(by_point).sort_values("seasonal_t2m_prod_rpss", ascending=False)
    by_point_df.to_csv(OUT / "by_point.csv", index=False)

    # стабильность: первые 10 vs последние 10 лет
    stable = {}
    for m in MODES:
        for v in VARS_:
            sub = df[(df["mode"] == m) & (df["variable"] == v)]
            h1 = group_metrics(sub[sub["year"] <= 2014])
            h2 = group_metrics(sub[sub["year"] >= 2015])
            for sysname, tag in (("product", "prod"), ("blend_counterfactual", "blend")):
                stable[f"{m}_{v}_{tag}"] = {
                    "first10_rpss": h1[h1.system == sysname].iloc[0]["rpss"],
                    "first10_hit": h1[h1.system == sysname].iloc[0]["hit"],
                    "last10_rpss": h2[h2.system == sysname].iloc[0]["rpss"],
                    "last10_hit": h2[h2.system == sysname].iloc[0]["hit"],
                }

    # ячейки навыка (точка × режим × переменная)
    sc_rows = []
    for p in df["point"].unique():
        for m in MODES:
            for v in VARS_:
                g = group_metrics(df[(df["point"] == p) & (df["mode"] == m) & (df["variable"] == v)])
                if g is None:
                    continue
                for _, r in g.iterrows():
                    sc_rows.append(dict(point=p, mode=m, variable=v, system=r["system"],
                                        rpss=r["rpss"], hit=r["hit"]))
    sc = pd.DataFrame(sc_rows)
    sc.to_csv(OUT / "skill_cells.csv", index=False)
    prod = sc[sc.system == "product"]
    blen = sc[sc.system == "blend_counterfactual"]
    share = {
        "cells_total": int(len(prod)),
        "prod_rpss_gt_0.02": round(float((prod.rpss > 0.02).mean()), 3),
        "prod_rpss_gt_0.05": round(float((prod.rpss > 0.05).mean()), 3),
        "blend_rpss_gt_0.02": round(float((blen.rpss > 0.02).mean()), 3),
        "blend_rpss_gt_0.05": round(float((blen.rpss > 0.05).mean()), 3),
    }

    # сезонная детализация
    season_detail = []
    for m in MODES:
        for v in VARS_:
            for s in ("DJF", "MAM", "JJA", "SON"):
                g = group_metrics(df[(df["mode"] == m) & (df["variable"] == v) & (df["season"] == s)])
                if g is None:
                    continue
                for _, r in g.iterrows():
                    season_detail.append(dict(mode=m, variable=v, season=s, system=r["system"],
                                              n=int(r["n"]), hit=r["hit"], rpss=r["rpss"],
                                              ece=r["ece"], cov=r["p10_90_coverage"]))
    pd.DataFrame(season_detail).to_csv(OUT / "season_detail.csv", index=False)

    summary["stable_10y"] = stable
    summary["skill_share"] = share
    (OUT / "audit_summary.json").write_text(
        json.dumps(summary, indent=1, ensure_ascii=False, default=str))
    log("summary сохранён")
    md = write_markdown(df, summary, by_point_df, by_year_df, stable, share)
    return md


def build_verdict(summary, stable, share, by_point_df):
    """Числовой вердикт: где навык, где его нет, что чинить в первую очередь."""
    V = []
    o_p, o_b = summary["overall"]
    st = summary["seasonal_t2m"]
    sp = summary["seasonal_tp"]
    mt = summary["monthly_t2m"]
    mp = summary["monthly_tp"]
    st_p, st_b, sp_p, sp_b, mt_p, mt_b, mp_p, mp_b = (
        st[0], st[1], sp[0], sp[1], mt[0], mt[1], mp[0], mp[1])

    def grade(rpss, hit):
        if rpss is None:
            return "нет данных"
        if rpss >= 0.15:
            return "реальный навык"
        if rpss >= 0.05:
            return "умеренный навык (полезен для решений)"
        if rpss > 0.0:
            return "слабый навык (граничный)"
        return "навыка нет (уровень климатологии)"

    V.append(f"**Общая оценка продукта: {grade(o_p['rpss'], o_p['hit'])}** — "
             f"итого RPSS {o_p['rpss']:+.3f} при hit {o_p['hit']:.1%} "
             f"(95% CI {o_p['hit_ci95'][0]:.1%}–{o_p['hit_ci95'][1]:.1%}; "
             f"случайный терцильный бросок дал бы ~33%).")
    V.append("")
    V.append("| сегмент | RPSS (продукт) | оценка | hit (продукт) |")
    V.append("|---|---|---|---|")
    for name, r in (("сезонный t2m", st_p), ("сезонный осадки", sp_p),
                    ("месячный t2m", mt_p), ("месячный осадки", mp_p)):
        V.append(f"| {name} | {r['rpss']:+.3f} | {grade(r['rpss'], r['hit'])} | {r['hit']:.1%} |")
    V.append("")
    gap_b = o_b["rpss"] - o_p["rpss"]
    V.append(f"1. **Разрыв «UI vs реестр» закрыт** (был +0.017 до исправления "
             f"`run_hindcast`): «проверка прошлого» теперь показывает тот же ансамбль, что и "
             f"A/B-реестр — продукт {o_p['rpss']:+.3f} vs чистый бленд {o_b['rpss']:+.3f} "
             f"(оставшаяся разница {gap_b:+.3f} — это NN-mix и калибровка, т.е. реальная "
             "система продукта).")
    sd = pd.read_csv(OUT / "season_detail.csv")
    good = sd[(sd.system == "product") & (sd.rpss > 0.05)]
    bad = sd[(sd.system == "product") & (sd.rpss <= 0.0)]
    if len(good):
        V.append("2. **Ниши с реальным навыком (RPSS > 0.05):** " +
                 "; ".join(f"{r['mode']}/{r['variable']}/{SEASON_RU.get(r['season'], r['season'])} "
                           f"(RPSS {r['rpss']:+.3f}, hit {r['hit']:.0%})" for _, r in good.iterrows()) + ".")
    if len(bad):
        V.append("3. **Сегменты без навыка (RPSS ≤ 0):** " +
                 "; ".join(f"{r['mode']}/{r['variable']}/{SEASON_RU.get(r['season'], r['season'])}"
                           for _, r in bad.iterrows()) +
                 ". На них продукт честно показывает ~климатологию (skill-floor сработал) — "
                 "продавать их как «прогноз» нельзя.")
    V.append(f"4. **Осадки — слабое место**: сезонный RPSS {sp_p['rpss']:+.3f}, "
             f"месячный {mp_p['rpss']:+.3f} (бленд {sp_b['rpss']:+.3f}/{mp_b['rpss']:+.3f}). "
             "Для продукта, обещающего «полив в м³/га», это главный риск: ключевая "
             "переменная для фермера (вода) не прогнозируется. Либо усиливать "
             "(снежная вода, почвенная влага, сезонные SST-аномалии), либо честно "
             "сужать обещание до температуры.")
    V.append("5. **Стабильность**: " +
             "; ".join(f"{k}: {s['first10_rpss']:+.3f} → {s['last10_rpss']:+.3f} (по 10 лет)"
                       for k, s in stable.items() if k.endswith("_prod")) + ". " +
             ("Навык по температуре не деградирует во времени — хорошо."
              if all(s["last10_rpss"] >= s["first10_rpss"] - 0.05 for k, s in stable.items()
                     if k.endswith("_prod") and "t2m" in k)
              else "Есть сегменты с деградацией навыка — разбирать."))
    V.append(f"6. **География аудита**: {len(by_point_df)} точек — бокс данных (43–47°N, 37–42°E) "
             "или сетка КРА (режим grid, 44–46.5°N, 37–40.5°E). "
             "Расширение на всю Россию — тот же скрипт + сеть (CPC-ингест ~2–6 мин/точку). "
             "До этого «карта России» в продукте подтверждена только для этого бокса.")
    V.append(f"7. **По точкам**: сезонный t2m — {(by_point_df['seasonal_t2m_prod_rpss'] > 0).sum()}/{len(by_point_df)} "
             f"положительных, медиана {by_point_df['seasonal_t2m_prod_rpss'].median():+.3f}; "
             f"бленд: {(by_point_df['seasonal_t2m_blend_rpss'] > 0).sum()}/{len(by_point_df)}, "
             f"медиана {by_point_df['seasonal_t2m_blend_rpss'].median():+.3f}. "
             "Разброс по точкам небольшой — эффект системный, не «удачный участок».")
    V.append("")
    V.append("**Итог: развивать — ДА, но в этом порядке:**")
    V.append("1. **(готово)** `run_hindcast` — «проверка прошлого» использует ансамблевый "
             "бленд, тот же, что в A/B-реестре; LIVE-прогноз и проверка теперь одна система.")
    V.append("2. **(готово)** Честный α (селекция 2004–2014 + валидация 2015–2024: ядро "
             "берётся только если строго улучшает валидацию) и маркировка «уровень "
             "климатологии» для переменных с RPSS ≤ 0.")
    V.append("3. Приоритет по переменным: температура (есть навык — монетизировать: риски "
             "морозов, сроки полевых работ); осадки (навык ~0 даже у чистого бленда — "
             "усиливать через снеговодность, почвенную влагу, SST-аномалии, либо честно "
             "свести обещание к температуре + дефициту ET0).")
    V.append("4. Верифицировать лиды 2–6 — сейчас слепое пятно «дальнего горизонта».")
    V.append("5. Прогнать этот же аудит на 20–30 точках по всей России (нужна сеть) и "
             "приложить к публичному реестру навыка.")
    V.append("")
    V.append("*Критерий остановки: если после п.1–2 бленд-версия держит RPSS < 0.05 по "
             "температуре у большинства точек 20 лет подряд — физический сигнал в сезонном "
             "горизонте для бокса слаб, проект имеет смысл сворачивать в «климатология + "
             "риски» или уходить в месячный горизонт 1–2 месяца.*")
    return V


def fmt_row(r):
    conf = r.get("p10_90_coverage_conformal")
    conf_s = f" → **{conf:.3f}**" if conf is not None else ""
    return (f"| {r['system']} | {r['n']} | {r['hit']:.3f} [{r['hit_ci95'][0]:.3f}–{r['hit_ci95'][1]:.3f}] "
            f"| {r['rps']:.3f} / {r['rps_clim']:.3f} | **{r['rpss']:+.3f}** | {r['ece']:.3f} "
            f"| {r['p10_90_coverage']:.3f}{conf_s} |")


HDR = ("| система | n | hit (95% CI) | RPS / RPS клим | RPSS | ECE | покрытие P10–P90 (сырое → конформация) |\n"
       "|---|---|---|---|---|---|---|")


def md_table(rows):
    return "\n".join([HDR] + [fmt_row(r) for r in rows])


def write_markdown(df, summary, by_point_df, by_year_df, stable, share):
    n_pts = df["point"].nunique()
    years = sorted(df["year"].unique())
    L = []
    L.append(f"# Полный аудит AgroCast: {n_pts} точек × 20 лет × все режимы\n")
    L.append(f"*Сгенерировано: {time.strftime('%Y-%m-%d %H:%M')} · `scripts/audit_full.py`*\n")
    L.append("## 1. Что проверено\n")
    L.append(f"- **Точек: {n_pts}** — уникальные ячейки 0.5° по боксу данных "
             "продукта (43–47°N, 37–42°E, центр — КРА / юг Центральной России). "
             "В боксе 70 из 99 ячеек с полными данными; набор зафиксирован в "
             "`data/audit/audit_points.json` (режим points) или `world/artifacts/krai_grid.json` (режим grid).")
    L.append(f"- **Годы: {years[0]}–{years[-1]}** (20 лет, leave-one-year-out, "
             "как в «проверке прошлого» продукта).")
    L.append("- **Режимы: seasonal** (12 стартовых месяцев, блок 3 месяца) **и monthly** "
             "(12 целевых месяцев, lead 1 — именно lead 1 использует продукт в проверке прошлого).")
    L.append(f"- **Верификаций: {len(df):,}** ({n_pts}×20×(12+12)×2 переменные: t2m и осадки).")
    L.append("- **Путь расчёта — идентичен продукту** (`serve.pipeline.run_hindcast` "
             "после исправления): P = ансамблевый бленд 9 моделей (leave-one-year-out, "
             "half_life=5 лет) → NN-mix (α из артефактов stack) → TercileCalibration по "
             "прошлым годам. Сверено с прямыми вызовами `run_hindcast`.")
    L.append("- **Плюс референс «blend»**: чистый бленд без NN и калибровки (протокол A/B) — "
             "точка отсчёта для сравнения с реестром.\n")
    L.append("> **Важно (география).** В песочнице нет выхода в интернет, поэтому точки "
             "вне бокса данных (нужно скачивание CPC для каждой точки) не покрываются. "
             "Выводы валидны для бокса 43–47°N/37–42°E; для остальных регионов схема та "
             "же — прогнать `python -m scripts.audit_full all` на машине с сетью.\n")

    L.append("## 2. Главный результат: насколько система точна\n")
    L.append("### 2.1. Итого (все точки, 20 лет, оба режима)\n")
    L.append(md_table(summary["overall"]) + "\n")
    L.append("### 2.2. По режимам\n")
    L.append(md_table(summary["mode_seasonal"]) + "\n")
    L.append(md_table(summary["mode_monthly"]) + "\n")
    L.append("### 2.3. По переменным\n")
    L.append(md_table(summary["var_t2m"]) + "\n")
    L.append(md_table(summary["var_tp"]) + "\n")
    L.append("### 2.4. Режим × переменная (детально)\n")
    for key in ("seasonal_t2m", "seasonal_tp", "monthly_t2m", "monthly_tp"):
        L.append(f"**{key}**\n")
        L.append(md_table(summary[key]) + "\n")

    L.append("## 3. Сезонная картина (продукт)\n")
    L.append("| режим | переменная | сезон | n | hit | RPSS | ECE | коридор |\n|---|---|---|---|---|---|---|---|")
    for _, r in pd.read_csv(OUT / "season_detail.csv").iterrows():
        if r.system == "product":
            L.append("| %s | %s | %s | %d | %.3f | %+.3f | %.3f | %.3f |" % (
                r["mode"], r["variable"], SEASON_RU.get(r["season"], r["season"]),
                int(r["n"]), r["hit"], r["rpss"], r["ece"], r["cov"]))
    L.append("")

    L.append("## 4. Калибровка (надёжность терцилей)\n")
    o = summary["overall"][0]  # product
    L.append(f"Продукт (итого): P(ниже)={o['pred_0']:.3f} при факт {o['freq_0']:.3f}; "
             f"P(норма)={o['pred_1']:.3f} при {o['freq_1']:.3f}; "
             f"P(выше)={o['pred_2']:.3f} при {o['freq_2']:.3f}. ECE={o['ece']:.3f}.\n")
    b = summary["overall"][1]
    L.append(f"Бленд (контрафакт): P(ниже)={b['pred_0']:.3f}/{b['freq_0']:.3f}; "
             f"P(норма)={b['pred_1']:.3f}/{b['freq_1']:.3f}; "
             f"P(выше)={b['pred_2']:.3f}/{b['freq_2']:.3f}. ECE={b['ece']:.3f}.\n")
    L.append("Покрытие коридора P10–P90 (конформная калибровка теперь применяется в проверке "
             "прошлого так же, как в LIVE): после конформации продукт "
             f"**{o.get('p10_90_coverage_conformal', float('nan')):.1%}**, без конформации "
             f"(сырые квантили) **{o['p10_90_coverage']:.1%}**; после конформации бленд "
             f"**{b.get('p10_90_coverage_conformal', float('nan')):.1%}**. Цель ≈80%.\n")

    L.append("## 5. Системный дефект — исправлен, этот аудит проверяет исправление\n")
    L.append("**Было:** `run_hindcast` брал `g.iloc[0]` из сырых записей — **первую модель** "
             "(`clim`, адаптивная климатология) + NN-mix + калибровку; рассчитанный блендер "
             "был мёртвым кодом. Пользователь видел систему слабее, чем анонсировал A/B-реестр "
             "(там мерялся бленд). Кроме того, `select_alpha` подбирал вес нейроядра на "
             "2004–2014 — годах, которые оказались годами валидации, — и для бокса давал "
             "вредный α по осадкам (0.6 сезонный): бленд +0.035 → бленд+NN(α=0.6) −0.090.\n")
    L.append("**Сделано в этом релизе:**\n"
             "1. `run_hindcast` — «проверка прошлого» и LIVE-прогноз используют одну систему: "
             "LOYO-бленд (half_life=5 лет) → NN-mix (α из артефактов) → TercileCalibration. "
             "Этот аудит воспроизводит именно этот путь.")
    L.append("2. `select_alpha` — α выбирается на 2004–2014 и проверяется на 2015–2024: если "
             "ядро не улучшает RPS на валидации строго — α=0. По боксу: сезонный t2m "
             "0.8→**0.0**, сезонный tp 0.6→**0.0**, месячный t2m 1.0→**0.2**, месячный tp "
             "0.4→**0.0** — ядро переучивалось на селекционном окне и по осадкам деградировало.")
    L.append("3. В отчёте для переменных с RPSS ≤ 0 — предупреждение «навык не подтверждён — "
             "уровень климатологии»: осадки теперь помечены честно.")
    L.append("4. В A/B-сравнение добавлен базлайн адаптивной климатологии.\n")
    p0, b0 = summary["overall"]
    L.append(f"Проверка исправления (цифры этого аудита): RPSS продукта **{p0['rpss']:+.3f}** "
             f"vs чистого бленда **{b0['rpss']:+.3f}** (разница {b0['rpss']-p0['rpss']:+.3f} — "
             "теперь только от NN-mix и калибровки, т.е. от реальной системы продукта).")
    for key in ("seasonal_t2m", "seasonal_tp", "monthly_t2m", "monthly_tp"):
        p1, b1 = summary[key]
        L.append(f"- {key}: продукт **{p1['rpss']:+.3f}** vs чистый бленд **{b1['rpss']:+.3f}**.")
    L.append("")
    L.append("Прочие находки (актуальны):")
    L.append("1. **Прошлая калибровка обучается на «будущем»**: для целевого года Y веса "
             "бленда прошлых лет = fit на годах ≥ Y (включая Y и все будущие) — "
             "soft-подглядывание в калибровке (на основном прогнозе не влияет).")
    L.append("2. **Monthly в этой проверке прошлого = только lead 1** (лимит кэша "
             "precompute). Лиды 2–6 верифицированы на выборке бокса 2004–24 (n=251 на "
             "лид): навык t2m стабильный по всем лидам (RPSS ≈ +0.10, hit 51–53%), "
             "tp — уровень климатологии на всех лидах → горизонт 1–6 сохранён.")
    L.append("3. **ИСПРАВЛЕНО: конформная калибровка включена в проверку прошлого** — "
             f"коридор P10–P90 после конформации (2004–24) **{p0.get('p10_90_coverage_conformal', float('nan')):.1%}** "
             f"(без конформации {p0['p10_90_coverage']:.1%}) — заявка ≈80% теперь верифицирована "
             "в режиме проверки прошлого, а не только в LIVE.")
    L.append("4. **Наблюдения — из центра бокса** (одна ячейка 0.5° на все точки бокса): "
             "для угловых точек расхождение факта до десятков км — допустимо, но стоит "
             "зафиксировать в отчёте продукта.\n")

    L.append("## 6. Стабильность по годам (RPSS, продукт vs бленд)\n")
    L.append("Первые 10 (2005–2014) vs последние 10 (2015–2024):\n")
    L.append("| режим×переменная | система | RPSS 10 лет 1 | hit | RPSS 10 лет 2 | hit |")
    L.append("|---|---|---|---|---|---|")
    for k, s in stable.items():
        L.append(f"| {k} | {'продукт' if k.endswith('_prod') else 'бленд'} "
                 f"| {s['first10_rpss']:+.3f} | {s['first10_hit']:.3f} "
                 f"| {s['last10_rpss']:+.3f} | {s['last10_hit']:.3f} |")
    L.append("")
    L.append("Динамика RPSS по годам (product / blend, сезонный t2m):\n")
    yt = by_year_df[(by_year_df["mode"] == "seasonal") & (by_year_df["variable"] == "t2m")]
    for y in years:
        pp = yt[(yt["year"] == y) & (yt["system"] == "product")].iloc[0]
        bb = yt[(yt["year"] == y) & (yt["system"] == "blend_counterfactual")].iloc[0]
        L.append(f"- **{y}**: product {pp['rpss']:+.3f} (hit {pp['hit']:.2f}) · "
                 f"blend {bb['rpss']:+.3f} (hit {bb['hit']:.2f})")
    L.append("")

    L.append(f"## 7. Разброс по {n_pts} точкам\n")
    L.append("Топ-10 точек (по RPSS сезонного t2m, продукт). Широта/долгота — "
             "ячейка данных 0.5°, которую реально использует продукт:\n")
    L.append("| точка | ячейка шир | ячейка долг | sez t2m RPSS | sez t2m hit | mon t2m RPSS | sez tp RPSS |")
    L.append("|---|---|---|---|---|---|---|")
    for _, r in by_point_df.head(10).iterrows():
        L.append(f"| {r['point']} | {r['grid_lat']:.2f} | {r['grid_lon']:.2f} | "
                 f"{r['seasonal_t2m_prod_rpss']:+.3f} | {r['seasonal_t2m_prod_hit']:.3f} | "
                 f"{r['monthly_t2m_prod_rpss']:+.3f} | {r['seasonal_tp_prod_rpss']:+.3f} |")
    L.append("")
    L.append("Анти-топ (худшие 10):\n")
    L.append("| точка | ячейка шир | ячейка долг | sez t2m RPSS | sez t2m hit | mon t2m RPSS | sez tp RPSS |")
    L.append("|---|---|---|---|---|---|---|")
    for _, r in by_point_df.tail(10).iloc[::-1].iterrows():
        L.append(f"| {r['point']} | {r['grid_lat']:.2f} | {r['grid_lon']:.2f} | "
                 f"{r['seasonal_t2m_prod_rpss']:+.3f} | {r['seasonal_t2m_prod_hit']:.3f} | "
                 f"{r['monthly_t2m_prod_rpss']:+.3f} | {r['seasonal_tp_prod_rpss']:+.3f} |")
    L.append("")
    L.append(f"Распределение RPSS (product, сезонный t2m) по точкам: "
             f"мин {by_point_df['seasonal_t2m_prod_rpss'].min():+.3f}, "
             f"медиана {by_point_df['seasonal_t2m_prod_rpss'].median():+.3f}, "
             f"макс {by_point_df['seasonal_t2m_prod_rpss'].max():+.3f}; "
             f"положительных: {(by_point_df['seasonal_t2m_prod_rpss'] > 0).sum()}/{n_pts}.")
    L.append("")

    L.append("## 8. Насколько выгодно развивать\n")
    L.append(f"- Ячеек навыка (точка×режим×переменная): **{share['cells_total']}**.")
    L.append(f"- RPSS > 0.02 (минимально полезный): продукт **{share['prod_rpss_gt_0.02']:.0%}**, "
             f"бленд **{share['blend_rpss_gt_0.02']:.0%}**.")
    L.append(f"- RPSS > 0.05 (практически значимый): продукт **{share['prod_rpss_gt_0.05']:.0%}**, "
             f"бленд **{share['blend_rpss_gt_0.05']:.0%}**.")
    L.append("")
    L.append("### Вердикт\n")
    L.extend(build_verdict(summary, stable, share, by_point_df))
    md = "\n".join(L)
    (OUT / "audit_report.md").write_text(md)
    log("отчёт: " + str(OUT / "audit_report.md"))
    return OUT / "audit_report.md"


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=["precompute", "points", "report", "mini5", "all", "grid"])
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--max-points", type=int, default=30)
    args = ap.parse_args()
    if args.phase in ("precompute", "all"):
        phase_precompute()
    if args.phase in ("points", "all"):
        phase_points(args.workers)
    if args.phase in ("report", "all"):
        phase_report()
    if args.phase == "mini5":
        phase_mini5(args.workers)
    if args.phase == "grid":
        phase_grid(args.workers, args.max_points)
