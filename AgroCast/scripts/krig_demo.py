# -*- coding: utf-8 -*-
"""Кринг-демо: live-прогноз 28 точек сетки КРА → поле терцилей осадков 0.25°.

Путь продукта без изменений: для каждой ячейки krai_grid.json —
forecast_point (сезонный режим, первый блок = целевой сезон), из ответа
берутся терцильные вероятности ОСАДКОВ (below/normal/above). Затем
обыкновенный кринг (agrocast/region/kriging.py: экспоненциальная
вариограмма, детрендинг плоскостью, IDW-фолбэк) строит поле вероятностей
на сетке 0.25° по КРА; вероятности после кринга обрезаются в [0, 1] и
перенормируются. Каждая карта сопровождается честной пометкой: навык
осадков на сетке КРА аудитом не подтверждён (уровень климатологии) —
поле демонстрирует связность прогноза, а не подтверждённый навык.

Запуск (из папки AgroCast):
    python -m scripts.krig_demo                       # старт 2026-10 → окт-дек 2026
    python -m scripts.krig_demo --start 2026-10 --workers 2

Результат: data/forecast/krig_demo_<start>.json / .csv / .md + лог в консоль.
На 2 ядрах полный прогон (28 точек × полный путь продукта + кринг) ≈ 6 минут.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

WORLD = str(BASE / "world")
DATA_ROOT = str(BASE / "data")
GRID_JSON = Path(WORLD) / "artifacts" / "krai_grid.json"
OUT_DIR = Path(DATA_ROOT) / "forecast"
TARGET_CELL = 0.25  # сетка кринг-поля, градусы

TERCILE_KEYS = ("below", "normal", "above")


def log(msg):
    print(f"[krig_demo] {time.strftime('%H:%M:%S')} {msg}", flush=True)


def target_grid(bounds, cell=TARGET_CELL):
    """Центры ячеек кринг-поля 0.25° внутри бокса КРА."""
    lat_min, lat_max, lon_min, lon_max = bounds["lat_min"], bounds["lat_max"], bounds["lon_min"], bounds["lon_max"]
    lats = np.arange(lat_min + cell / 2, lat_max, cell)
    lons = np.arange(lon_min + cell / 2, lon_max, cell)
    return lats, lons


def forecast_one(args_):
    """Полный путь LIVE-прогноза продукта для одной ячейки сетки."""
    pid, lat, lon, start = args_
    from agrocast.forecast.orchestrator import forecast_point
    from agrocast.serve.pipeline import point_config

    t0 = time.time()
    cfg, _ = point_config(WORLD, DATA_ROOT, lat, lon)
    payload = forecast_point(
        cfg, lat, lon,
        start=start, horizon=3, mode="seasonal", season_len=3,
        save=False, variables=("t2m", "tp"),
    )
    it = (payload.get("seasons") or [None])[0]
    if not it or "tp" not in it:
        raise RuntimeError(f"{pid}: прогноз без блока осадков")
    probs = it["tp"]["tercile_probs"]
    months = it.get("months") or []
    out = {
        "id": pid, "lat": float(lat), "lon": float(lon),
        "target": months[0] if months else start,
        "months": months,
        "below": float(probs["below"]),
        "normal": float(probs["normal"]),
        "above": float(probs["above"]),
        "p50_mm": (it["tp"].get("quantiles_mm") or {}).get("p50"),
        "normal_mm": it["tp"].get("normal_mm"),
        "issue_through": payload.get("issue_data_through"),
        "t": round(time.time() - t0, 1),
    }
    log(f"{pid} ({lat:.2f},{lon:.2f}): below/normal/above = "
        f"{out['below']:.2f}/{out['normal']:.2f}/{out['above']:.2f} ({out['t']}s)")
    return out


def krige_field(points, bounds):
    """Кринг терцилей осадков на сетку 0.25°: по каждой вероятности отдельно,
    затем обрезка в [0,1] и перенормировка суммы в 1."""
    from agrocast.region.kriging import krige

    lats, lons = target_grid(bounds)
    targets = np.column_stack([np.repeat(lats, len(lons)), np.tile(lons, len(lats))])
    pts = np.array([(p["lat"], p["lon"]) for p in points])
    field = []
    vg_out = None
    comps = {}
    for k in TERCILE_KEYS:
        vals = np.array([p[k] for p in points])
        res = krige(pts, vals, targets, detrend=False)
        comps[k] = np.clip(res.pred, 0.0, 1.0)
        if vg_out is None:
            vg_out = dict(nugget=round(res.variogram.nugget, 4),
                          psill=round(res.variogram.psill, 4),
                          range_km=round(res.variogram.range_km, 1),
                          n_fallback=int(res.n_fallback))
    tot = np.sum([comps[k] for k in TERCILE_KEYS], axis=0)
    tot = np.where(tot <= 1e-9, 1.0, tot)
    for i, (la, lo) in enumerate(targets):
        probs = {k: float(comps[k][i] / tot[i]) for k in TERCILE_KEYS}
        dom = max(TERCILE_KEYS, key=lambda k: probs[k])
        field.append({"lat": round(float(la), 3), "lon": round(float(lo), 3),
                      **{k: round(probs[k], 4) for k in TERCILE_KEYS}, "dominant": dom})
    return field, vg_out, lats, lons


SYMBOL = {"below": "↓", "normal": "=", "above": "↑"}


def ascii_map(field, lats, lons):
    """Текстовая карта доминирующей терцели (север сверху)."""
    look = {(round(f["lat"], 3), round(f["lon"], 3)): f["dominant"] for f in field}
    lines = ["     " + "".join(f"{lo:6.2f}" for lo in lons)]
    for la in lats[::-1]:
        row = "".join(f"{SYMBOL[look[(round(float(la), 3), round(float(lo), 3))]]:>6}" for lo in lons)
        lines.append(f"{la:5.2f} {row}")
    return "\n".join(lines)


def write_markdown(path, meta, field, lats, lons, points):
    art = json.loads(GRID_JSON.read_text())
    L = []
    L.append(f"# Кринг-поле терцилей осадков КРА: {meta['target_label']} (старт {meta['start']})\n")
    L.append(f"*Сгенерировано: {time.strftime('%Y-%m-%d %H:%M')} · `scripts/krig_demo.py` · "
             f"выпуск по данные {meta['issue_through']}*\n")
    L.append(f"- Вход: **{len(points)} точек** сетки КРА 0.5° (`world/artifacts/krai_grid.json`, "
             f"{art['n_cells']} из {art['n_candidates']} ячеек, покрытие ≥90%).")
    L.append("- Прогноз: полный путь LIVE-продукта (сезонный режим), терцили осадков "
             "первого блока.")
    L.append(f"- Поле: обыкновенный кринг на сетку **0.25°** ({len(lats)}×{len(lons)} = "
             f"{len(field)} ячеек), вариограмма экспоненциальная: nugget {meta['variogram']['nugget']}, "
             f"psill {meta['variogram']['psill']}, range {meta['variogram']['range_km']} км.\n")
    L.append("**Честная пометка:** навык сезонных осадков на сетке КРА аудитом "
             "(`audit_full grid`, 20 лет) **не подтверждён** — уровень климатологии. "
             "Поле показывает связность прогноза продукта, а не подтверждённый навык.\n")
    L.append("## Доминирующая терцель осадков (карта, север сверху)\n")
    L.append("```")
    L.append(ascii_map(field, lats, lons))
    L.append("```\n")
    L.append("## Точки сетки (вход кринга)\n")
    L.append("| точка | шир | долг | ниже | норма | выше | медиана, мм | норма, мм |")
    L.append("|---|---|---|---|---|---|---|---|")
    for p in points:
        L.append(f"| {p['id']} | {p['lat']:.2f} | {p['lon']:.2f} | {p['below']:.3f} | "
                 f"{p['normal']:.3f} | {p['above']:.3f} | {p.get('p50_mm') or '—'} | "
                 f"{p.get('normal_mm') or '—'} |")
    md = "\n".join(L)
    path.write_text(md, encoding="utf-8")
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-10", help="стартовый месяц сезона (YYYY-MM)")
    ap.add_argument("--workers", type=int, default=2)
    args = ap.parse_args()

    art = json.loads(GRID_JSON.read_text())
    pts = [(c["id"], float(c["lat"]), float(c["lon"]), args.start) for c in art["cells"]]
    log(f"кринг-демо: {len(pts)} точек КРА, старт {args.start}, рабочих {args.workers}")
    t0 = time.time()
    results = []
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        for r in ex.map(forecast_one, pts):
            results.append(r)
    t_fc = time.time() - t0
    log(f"прогнозы: {len(results)}/{len(pts)} за {t_fc:.0f}s; кринг…")

    t1 = time.time()
    field, vg, lats, lons = krige_field(results, art["bounds"])
    t_kr = time.time() - t1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    months = results[0]["months"]
    meta = {
        "engine": "agrocast-krig-demo",
        "start": args.start,
        "target": results[0]["target"],
        "target_label": "–".join(m[:7] for m in (months or [results[0]["target"]])),
        "months": months,
        "issue_through": results[0]["issue_through"],
        "n_points": len(results),
        "n_cells": len(field),
        "cell_deg": TARGET_CELL,
        "bounds": art["bounds"],
        "variogram": vg,
        "runtime_s": {"forecast": round(t_fc, 1), "kriging": round(t_kr, 1)},
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "disclaimer": "Навык осадков на сетке КРА не подтверждён (уровень климатологии); поле — демонстрация связности прогноза.",
    }
    payload = {"meta": meta, "points": results, "field": field}
    js = OUT_DIR / f"krig_demo_{args.start}.json"
    js.write_text(json.dumps(payload, ensure_ascii=False, indent=1))

    rows = []
    for f in field:
        rows.append(dict(lat=f["lat"], lon=f["lon"], below=f["below"],
                         normal=f["normal"], above=f["above"], dominant=f["dominant"]))
    pd.DataFrame(rows).to_csv(OUT_DIR / f"krig_demo_{args.start}.csv", index=False)
    md = write_markdown(OUT_DIR / f"krig_demo_{args.start}.md", meta, field, lats, lons, results)

    doms = pd.Series([f["dominant"] for f in field]).value_counts()
    log(f"поле {len(field)} ячеек 0.25° за {t_kr:.1f}s; доминирующая терцель: " +
        ", ".join(f"{k} {v}" for k, v in doms.items()))
    log("файлы: " + ", ".join(str(OUT_DIR / f"krig_demo_{args.start}{e}") for e in (".json", ".csv", ".md")))
    print()
    print(ascii_map(field, lats, lons))
    print()
    log(f"готово за {time.time()-t0:.0f}s; отчёт: {md}")
    return js


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
