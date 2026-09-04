from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

WORLD = str(BASE / "world")
DATA_ROOT = str(BASE / "data")
GRID_JSON = Path(WORLD) / "artifacts" / "krai_grid.json"
OUT_JSON = Path(DATA_ROOT) / "audit" / "krig_demo_tp.json"
TARGET_CELL = 0.25
TERCILE_KEYS = ("below", "normal", "above")


def log(msg):
    print(f"[krig_demo] {time.strftime('%H:%M:%S')} {msg}", flush=True)


def target_grid(bounds, cell=TARGET_CELL):
    lats = np.arange(bounds["lat_min"] + cell / 2, bounds["lat_max"], cell)
    lons = np.arange(bounds["lon_min"] + cell / 2, bounds["lon_max"], cell)
    return lats, lons


def forecast_one(args_):
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
    from agrocast.region.kriging import ordinary_kriging

    lats, lons = target_grid(bounds)
    targets = np.column_stack([np.repeat(lats, len(lons)), np.tile(lons, len(lats))])
    pts = np.array([(p["lat"], p["lon"]) for p in points])
    comps = {}
    vg_out = None
    fallback = 0
    for k in TERCILE_KEYS:
        vals = np.array([p[k] for p in points])
        res = ordinary_kriging(pts, vals, targets, detrend=False)
        comps[k] = np.clip(res.pred, 0.0, 1.0).reshape(len(lats), len(lons))
        fallback += int(res.n_fallback)
        if vg_out is None:
            vg_out = {"nugget": round(res.variogram.nugget, 4),
                      "psill": round(res.variogram.psill, 4),
                      "range_km": round(res.variogram.range_km, 1)}
    tot = sum(comps[k] for k in TERCILE_KEYS)
    tot = np.where(tot <= 1e-9, 1.0, tot)
    field = {k: np.round(comps[k] / tot, 4).tolist() for k in TERCILE_KEYS}
    return field, vg_out, fallback, lats, lons


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-10")
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
    field, vg, fallback, lats, lons = krige_field(results, art["bounds"])
    t_kr = time.time() - t1

    doms = {}
    bi = np.argmax(np.stack([np.asarray(field[k]) for k in TERCILE_KEYS]), axis=0)
    for i in bi.ravel().tolist():
        doms[TERCILE_KEYS[i]] = doms.get(TERCILE_KEYS[i], 0) + 1

    payload = {
        "meta": {
            "engine": "agrocast-krig-demo",
            "start": args.start,
            "target": results[0]["target"],
            "months": results[0]["months"],
            "issue_through": results[0]["issue_through"],
            "n_points": len(results),
            "grid_cell_deg": TARGET_CELL,
            "lats": [round(float(x), 3) for x in lats],
            "lons": [round(float(x), 3) for x in lons],
            "bounds": art["bounds"],
            "variogram": vg,
            "idw_fallback_cells": fallback,
            "dominant_cells": doms,
            "runtime_s": {"forecast": round(t_fc, 1), "kriging": round(t_kr, 1)},
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "disclaimer": "Навык осадков на сетке КРА не подтверждён (уровень климатологии); поле — демонстрация связности прогноза.",
        },
        "points": results,
        "field": field,
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=1))
    log(f"поле {len(lats) * len(lons)} ячеек 0.25° за {t_kr:.1f}s; доминирующая терцель: "
        + ", ".join(f"{k} {v}" for k, v in doms.items()))
    log(f"готово за {time.time() - t0:.0f}s → {OUT_JSON}")
    return OUT_JSON


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
