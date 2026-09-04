# -*- coding: utf-8 -*-
"""Сетка ячеек 0.5° по Краснодарскому краю (КРА) с фильтром покрытия данных.

КРА задаётся боксом 44–46.5°N, 37–40.5°E. Кандидаты — центры ячеек 0.5°
внутри бокса (5×7 = 35). Ячейка доступна, если обе переменные (t2m и tp)
заполнены не менее чем на 90% суток периода данных (тот же критерий,
что и в PointDataset.raw_daily: notna().mean() > 0.9). Ячейки у побережья
с неполными сериями отбрасываются; для текущих данных остаётся 28 ячеек.

Артефакт world/artifacts/krai_grid.json фиксирует набор точек — его
используют аудит сетки (scripts/audit_full.py grid) и кринг-демо
(scripts/krig_demo.py).
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

# Бокс Краснодарского края: (lat_min, lat_max, lon_min, lon_max)
KRA_BOUNDS = (44.0, 46.5, 37.0, 40.5)
CELL_DEG = 0.5                 # размер ячейки, градусы
MIN_COVERAGE = 0.90            # фильтр: доля не-NaN ≥ 90% у t2m и tp
DEFAULT_ARTIFACT = Path(__file__).resolve().parents[2] / "world" / "artifacts" / "krai_grid.json"


def cell_centers(bounds=KRA_BOUNDS, cell=CELL_DEG):
    """Центры всех ячеек `cell` внутри бокса (кандидаты до фильтра).

    Возвращает (lats, lons): одномерные массивы центров по широте и долготе.
    """
    lat_min, lat_max, lon_min, lon_max = (float(b) for b in bounds)
    lats = np.arange(lat_min + cell / 2.0, lat_max, cell)
    lons = np.arange(lon_min + cell / 2.0, lon_max, cell)
    return lats, lons


def coverage_from_daily(df):
    """Доля заполненных значений по каждой переменной ячейки.

    df — DataFrame с колонками t2m/tp (индекс — время). Возвращает
    dict {var: float доля не-NaN}; отсутствующая колонка даёт 0.0.
    """
    out = {}
    n = max(len(df), 1)
    for v in ("t2m", "tp"):
        out[v] = float(df[v].notna().sum()) / n if v in df.columns else 0.0
    out["coverage"] = min(out["t2m"], out["tp"])
    return out


def build_grid(world_dir, min_coverage=MIN_COVERAGE, cell=CELL_DEG, bounds=KRA_BOUNDS, ds=None):
    """Собрать сетку КРА: кандидаты → фильтр покрытия → артефакт.

    ds — опционально готовый xarray.Dataset с daily_region (для тестов);
    иначе открывается world/zarr/daily_region из world_dir.
    Точки сортируются с севера на юг, внутри строки — с запада на восток,
    id — G01..GNN.
    """
    if ds is None:
        import xarray as xr

        ds = xr.open_zarr(str(Path(world_dir) / "zarr" / "daily_region"))
    lats, lons = cell_centers(bounds, cell)
    t = pd.to_datetime(ds.time.values)
    cells = []
    for la in sorted(lats, reverse=True):
        for lo in lons:
            sub = ds[["t2m", "tp"]].sel(lat=float(la), lon=float(lo))
            df = pd.DataFrame({"t2m": sub["t2m"].values, "tp": sub["tp"].values}, index=t)
            cov = coverage_from_daily(df)
            cell_rec = {
                "lat": round(float(la), 2),
                "lon": round(float(lo), 2),
                "coverage_t2m": round(cov["t2m"], 4),
                "coverage_tp": round(cov["tp"], 4),
                "coverage": round(cov["coverage"], 4),
            }
            if cov["coverage"] >= float(min_coverage):
                cells.append(cell_rec)
    for i, c in enumerate(cells, 1):
        c["id"] = f"G{i:02d}"
    lat_min, lat_max, lon_min, lon_max = bounds
    return {
        "name": "krai_grid",
        "description": "Сетка ячеек 0.5° по Краснодарскому краю с покрытием данных ≥ {:.0%}".format(min_coverage),
        "cell_deg": cell,
        "bounds": {"lat_min": lat_min, "lat_max": lat_max, "lon_min": lon_min, "lon_max": lon_max},
        "min_coverage": float(min_coverage),
        "period": [str(t.min().date()), str(t.max().date())],
        "n_candidates": int(len(lats) * len(lons)),
        "n_cells": len(cells),
        "cells": cells,
    }


def save_grid(grid, path=DEFAULT_ARTIFACT):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(grid, ensure_ascii=False, indent=1))


def load_grid(path=DEFAULT_ARTIFACT):
    """Загрузить артефакт сетки (dict как в build_grid)."""
    return json.loads(Path(path).read_text())


def grid_points(grid):
    """Список точек сетки [(id, lat, lon), ...] в порядке артефакта."""
    return [(c["id"], float(c["lat"]), float(c["lon"])) for c in grid["cells"]]
