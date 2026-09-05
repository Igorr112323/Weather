from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

KRA_BOUNDS = (44.0, 46.5, 37.0, 40.5)
CELL_DEG = 0.5
MIN_COVERAGE = 0.90
from agrocast.core.settings import RuntimeSettings, DEFAULT_WORLD

DEFAULT_ARTIFACT = DEFAULT_WORLD / "artifacts/krai_grid.json"


def cell_centers(bounds=KRA_BOUNDS, cell=CELL_DEG):
    lat_min, lat_max, lon_min, lon_max = (float(b) for b in bounds)
    la0 = np.floor(lat_min / cell) * cell + cell / 2.0
    lo0 = np.floor(lon_min / cell) * cell + cell / 2.0
    lats = np.arange(la0, lat_max, cell)
    lons = np.arange(lo0, lon_max, cell)
    lats = lats[lats > lat_min]
    lons = lons[lons > lon_min]
    return lats, lons


def coverage_from_daily(df):
    out = {}
    n = max(len(df), 1)
    for v in ("t2m", "tp"):
        out[v] = float(df[v].notna().sum()) / n if v in df.columns else 0.0
    out["coverage"] = min(out["t2m"], out["tp"])
    return out


def krai_cells(store, min_coverage=MIN_COVERAGE, cell=CELL_DEG, bounds=KRA_BOUNDS):
    ds = store.open("daily_region")
    lats, lons = cell_centers(bounds, cell)
    t = pd.to_datetime(ds.time.values)
    store_lats = np.asarray(ds.lat.values, dtype=float)
    store_lons = np.asarray(ds.lon.values, dtype=float)
    cells = []
    for la in sorted(lats, reverse=True):
        for lo in lons:
            sla = _match_coord(store_lats, la)
            slo = _match_coord(store_lons, lo)
            if sla is None or slo is None:
                continue
            sub = ds[["t2m", "tp"]].sel(lat=sla, lon=slo)
            df = pd.DataFrame({"t2m": sub["t2m"].values, "tp": sub["tp"].values}, index=t)
            cov = coverage_from_daily(df)
            if cov["coverage"] >= float(min_coverage):
                cells.append({
                    "lat": round(float(la), 2),
                    "lon": round(float(lo), 2),
                    "coverage_t2m": round(cov["t2m"], 4),
                    "coverage_tp": round(cov["tp"], 4),
                    "coverage": round(cov["coverage"], 4),
                })
    for i, c in enumerate(cells, 1):
        c["id"] = f"P{i:02d}"
    return cells


def _match_coord(vals, x):
    i = int(np.argmin(np.abs(vals - float(x))))
    v = vals[i]
    return float(v) if np.isclose(v, float(x), atol=1e-6) else None


def save_grid(cells, path=None, bounds=KRA_BOUNDS, cell=CELL_DEG, min_coverage=MIN_COVERAGE, name="krai_grid"):
    lat_min, lat_max, lon_min, lon_max = (float(b) for b in bounds)
    cl, co = cell_centers(bounds, cell)
    grid = {
        "name": name,
        "cell_deg": cell,
        "bounds": {"lat_min": lat_min, "lat_max": lat_max, "lon_min": lon_min, "lon_max": lon_max},
        "min_coverage": float(min_coverage),
        "n_candidates": int(len(cl) * len(co)),
        "n_cells": len(cells),
        "cells": cells,
    }
    settings = RuntimeSettings.from_environment()
    path = settings.writable_path(path if path is not None else settings.state_dir / "compute/artifacts/krai_grid.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(grid, ensure_ascii=False, indent=1))
    return grid


def load_grid(path=None):
    return json.loads(Path(path or RuntimeSettings.from_environment().world_dir / "artifacts/krai_grid.json").read_text())


def grid_points(grid):
    return [(c["id"], float(c["lat"]), float(c["lon"])) for c in grid["cells"]]
