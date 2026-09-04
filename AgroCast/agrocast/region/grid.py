from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

KRA_BOUNDS = (44.0, 46.5, 37.0, 40.5)
CELL_DEG = 0.5
MIN_COVERAGE = 0.90
DEFAULT_ARTIFACT = Path(__file__).resolve().parents[2] / "world" / "artifacts" / "krai_grid.json"


def cell_centers(bounds=KRA_BOUNDS, cell=CELL_DEG):
    lat_min, lat_max, lon_min, lon_max = (float(b) for b in bounds)
    lats = np.arange(lat_min + cell / 2.0, lat_max, cell)
    lons = np.arange(lon_min + cell / 2.0, lon_max, cell)
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
    cells = []
    for la in sorted(lats, reverse=True):
        for lo in lons:
            sub = ds[["t2m", "tp"]].sel(lat=float(la), lon=float(lo))
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


def save_grid(cells, path=DEFAULT_ARTIFACT, bounds=KRA_BOUNDS, cell=CELL_DEG, min_coverage=MIN_COVERAGE):
    lat_min, lat_max, lon_min, lon_max = (float(b) for b in bounds)
    cl, co = cell_centers(bounds, cell)
    grid = {
        "name": "krai_grid",
        "cell_deg": cell,
        "bounds": {"lat_min": lat_min, "lat_max": lat_max, "lon_min": lon_min, "lon_max": lon_max},
        "min_coverage": float(min_coverage),
        "n_candidates": int(len(cl) * len(co)),
        "n_cells": len(cells),
        "cells": cells,
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(grid, ensure_ascii=False, indent=1))
    return grid


def load_grid(path=DEFAULT_ARTIFACT):
    return json.loads(Path(path).read_text())


def grid_points(grid):
    return [(c["id"], float(c["lat"]), float(c["lon"])) for c in grid["cells"]]
