from __future__ import annotations

import json
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

from agrocast.region.regions import REGIONS, grid_artifact_path, known, region_name, skill_artifact_path

TERCILE_KEYS = ("below", "normal", "above")
FIELD_CELL = 0.25
FIELD_MAX_AGE_S = 7 * 24 * 3600.0
REGION_REBUILD_AFTER_S = 20 * 24 * 3600.0
KRAI_FIELD_RELPATH = Path("audit") / "krig_demo_tp.json"


def known_region(region):
    return known(region)


def grid_path(world_dir, region="krai"):
    return grid_artifact_path(world_dir, region)


def skill_path(world_dir, region="krai"):
    return skill_artifact_path(world_dir, region)


def field_path(data_root, region="krai"):
    if region == "krai":
        return Path(data_root) / KRAI_FIELD_RELPATH
    return Path(data_root) / "audit" / f"{region}_field.json"


def field_age_s(data_root, region="krai"):
    p = field_path(data_root, region)
    if not p.exists():
        return None
    return max(0.0, time.time() - p.stat().st_mtime)


def grid_payload(world_dir, region="krai"):
    p = grid_path(world_dir, region)
    if not p.exists():
        return {"ok": False, "error": "артефакт сетки не найден", "region": region}
    g = json.loads(p.read_text(encoding="utf-8"))
    g["region"] = region
    g["region_name"] = region_name(region) if known_region(region) else region
    return {"ok": True, "grid": g}


def skill_payload(world_dir, region="krai"):
    p = skill_path(world_dir, region)
    if not p.exists():
        return {"ok": False, "error": "артефакт навыка не найден", "region": region}
    return {"ok": True, "skill": json.loads(p.read_text(encoding="utf-8"))}


def field_payload(data_root, region="krai"):
    p = field_path(data_root, region)
    if not p.exists():
        return {"ok": False, "error": "поле ещё не рассчитано — нажмите «Рассчитать поле»", "region": region}
    d = json.loads(p.read_text(encoding="utf-8"))
    meta = dict(d.get("meta") or {})
    meta["region"] = region
    meta["region_name"] = region_name(region) if known_region(region) else region
    age_s = field_age_s(data_root, region)
    meta["age_s"] = int(age_s)
    meta["stale"] = bool(age_s > FIELD_MAX_AGE_S)
    return {"ok": True, "meta": meta, "points": d.get("points") or [], "field": d.get("field") or {}}


def dominant_counts(payload):
    meta = dict((payload or {}).get("meta") or {})
    if meta.get("dominant_cells"):
        return {k: int(v) for k, v in meta["dominant_cells"].items()}
    f = (payload or {}).get("field") or {}
    counts = {k: 0 for k in TERCILE_KEYS}
    comps = [np.asarray(f.get(k) or [], dtype=float) for k in TERCILE_KEYS]
    if comps[0].size and comps[0].shape == comps[1].shape == comps[2].shape:
        bi = np.argmax(np.stack(comps), axis=0)
        for i in bi.ravel().tolist():
            counts[TERCILE_KEYS[i]] = counts[TERCILE_KEYS[i]] + 1
    return counts


def region_block(world_dir, data_root, region="krai"):
    if not known_region(region):
        return ""
    lines = [f"Регион · {region_name(region)} ({region})"]
    gp = grid_path(world_dir, region)
    if not gp.exists():
        lines.append("сетка региона не рассчитана")
        return "\n".join(lines)
    g = json.loads(gp.read_text(encoding="utf-8"))
    lines.append(f"сетка: {g['n_cells']} ячеек 0.5° ({g['bounds']['lat_min']}–{g['bounds']['lat_max']}°N, "
                 f"{g['bounds']['lon_min']}–{g['bounds']['lon_max']}°E)")
    fp = field_path(data_root, region)
    if fp.exists():
        d = json.loads(fp.read_text(encoding="utf-8"))
        meta = d.get("meta") or {}
        months = meta.get("months") or []
        if months:
            head, tail = str(months[0]), str(months[-1])
            lines.append(f"поле: месяцы {head}–{tail}" + (f", выпуск по данным {meta.get('issue_through')}" if meta.get("issue_through") else ""))
        dc = dominant_counts(d)
        if any(dc.values()):
            lines.append("доминирующая терцель (ячейки 0.25°): " + ", ".join(
                f"{k} {v}" for k, v in dc.items() if v))
        age = field_age_s(data_root, region)
        if age is not None:
            lines.append(f"поле рассчитано {int(age // 3600)} ч назад")
    else:
        lines.append("поле региона не рассчитано")
    sp = skill_path(world_dir, region)
    if sp.exists():
        s = json.loads(sp.read_text(encoding="utf-8"))
        st = s.get("seasonal_t2m") or {}
        rpss = st.get("rpss")
        hit = st.get("hit")
        if rpss is not None:
            t = f"навык t2m (сезон, {s.get('years', '2005-2024')}, {s.get('n_points', '?')} точек): RPSS {rpss:+.3f}"
            if hit is not None:
                t += f", попадания {100 * hit:.0f}%"
            lines.append(t)
        note = s.get("note_tp") or "осадки: навык не подтверждён — уровень климатологии"
        lines.append(note)
    else:
        lines.append("артефакт навыка отсутствует: навык t2m и осадков не подтверждён")
    lines.append("честно: поле осадков — связность прогноза продукта, не подтверждённый навык")
    return "\n".join(lines)


def _forecast_cell(args_):
    pid, lat, lon, start, world_dir, data_root = args_
    from agrocast.forecast.orchestrator import forecast_point
    from agrocast.serve.pipeline import point_config

    cfg, _ = point_config(world_dir, data_root, lat, lon)
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
    return {
        "id": pid, "lat": float(lat), "lon": float(lon),
        "target": months[0] if months else start,
        "months": months,
        "below": float(probs["below"]),
        "normal": float(probs["normal"]),
        "above": float(probs["above"]),
        "p50_mm": (it["tp"].get("quantiles_mm") or {}).get("p50"),
        "normal_mm": it["tp"].get("normal_mm"),
        "issue_through": payload.get("issue_data_through"),
    }


def build_field(start, world_dir, data_root, region="krai", workers=2, log=None):
    from agrocast.region.kriging import ordinary_kriging

    if not known_region(region):
        raise ValueError(f"неизвестный регион: {region}")
    say = log or (lambda m: None)
    art = json.loads(grid_path(world_dir, region).read_text(encoding="utf-8"))
    cells = art["cells"]
    jobs = [(c["id"], float(c["lat"]), float(c["lon"]), start, str(world_dir), str(data_root))
            for c in cells]
    t0 = time.time()
    results = []
    with ProcessPoolExecutor(max_workers=int(workers)) as ex:
        for r in ex.map(_forecast_cell, jobs):
            results.append(r)
            say(f"{r['id']} ({r['lat']:.2f},{r['lon']:.2f}): "
                f"{r['below']:.2f}/{r['normal']:.2f}/{r['above']:.2f} "
                f"[{len(results)}/{len(jobs)}]")
    say(f"прогнозы: {len(results)}/{len(jobs)} за {time.time() - t0:.0f}s; кринг…")
    t1 = time.time()
    b = art["bounds"]
    lats = np.arange(b["lat_min"] + FIELD_CELL / 2, b["lat_max"], FIELD_CELL)
    lons = np.arange(b["lon_min"] + FIELD_CELL / 2, b["lon_max"], FIELD_CELL)
    targets = np.column_stack([np.repeat(lats, len(lons)), np.tile(lons, len(lats))])
    pts = np.array([(p["lat"], p["lon"]) for p in results])
    comps = {}
    vg_out = {}
    fallback = 0
    for k in TERCILE_KEYS:
        vals = np.array([p[k] for p in results])
        res = ordinary_kriging(pts, vals, targets, detrend=False)
        comps[k] = np.clip(res.pred, 0.0, 1.0).reshape(len(lats), len(lons))
        fallback += int(res.n_fallback)
        vg_out = {"nugget": round(res.variogram.nugget, 4),
                  "psill": round(res.variogram.psill, 4),
                  "range_km": round(res.variogram.range_km, 1)}
    tot = sum(comps[k] for k in TERCILE_KEYS)
    tot = np.where(tot <= 1e-9, 1.0, tot)
    field = {k: np.round(comps[k] / tot, 4).tolist() for k in TERCILE_KEYS}
    doms = {}
    bi = np.argmax(np.stack([np.asarray(field[k]) for k in TERCILE_KEYS]), axis=0)
    for i in bi.ravel().tolist():
        doms[TERCILE_KEYS[i]] = doms.get(TERCILE_KEYS[i], 0) + 1
    payload = {
        "meta": {
            "engine": "agrocast-region-field",
            "region": region,
            "region_name": region_name(region),
            "start": start,
            "target": results[0]["target"],
            "months": results[0]["months"],
            "issue_through": results[0]["issue_through"],
            "n_points": len(results),
            "grid_cell_deg": FIELD_CELL,
            "lats": [round(float(x), 3) for x in lats],
            "lons": [round(float(x), 3) for x in lons],
            "bounds": b,
            "variogram": vg_out,
            "idw_fallback_cells": fallback,
            "dominant_cells": doms,
            "runtime_s": {"forecast": round(time.time() - t0, 1), "kriging": round(time.time() - t1, 1)},
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "disclaimer": f"Навык осадков на сетке региона не подтверждён (уровень климатологии); поле — связность прогноза продукта, не подтверждённый навык.",
        },
        "points": results,
        "field": field,
    }
    fp = field_path(data_root, region)
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    say(f"поле {len(lats) * len(lons)} ячеек 0.25° за {time.time() - t1:.1f}s; доминирующая терцель: "
        + ", ".join(f"{k} {v}" for k, v in doms.items()))
    return fp
