# -*- coding: utf-8 -*-
"""Тесты сетки КРА: кандидаты, фильтр покрытия, артефакт."""
import json

import numpy as np
import pandas as pd

from agrocast.region.grid import (
    KRA_BOUNDS,
    cell_centers,
    coverage_from_daily,
    build_grid,
    load_grid,
    grid_points,
)


def test_centers_in_bounds_and_count():
    lats, lons = cell_centers()
    lat_min, lat_max, lon_min, lon_max = KRA_BOUNDS
    assert len(lats) * len(lons) == 35          # 5 × 7 кандидатов
    assert (lats > lat_min).all() and (lats < lat_max).all()
    assert (lons > lon_min).all() and (lons < lon_max).all()
    assert np.allclose(np.diff(lats), 0.5) and np.allclose(np.diff(lons), 0.5)
    # центры лежат на серединах ячеек
    assert abs(lats[0] - (lat_min + 0.25)) < 1e-9
    assert abs(lons[0] - (lon_min + 0.25)) < 1e-9


def test_coverage_from_daily():
    idx = pd.period_range("2000-01", "2000-12", freq="D").to_timestamp()
    full = pd.DataFrame({"t2m": np.arange(len(idx), dtype=float), "tp": 1.0}, index=idx)
    cov = coverage_from_daily(full)
    assert cov["t2m"] == 1.0 and cov["tp"] == 1.0 and cov["coverage"] == 1.0
    half = full.copy()
    half.loc[idx[: len(idx) // 2], "tp"] = np.nan       # половина tp — NaN
    cov2 = coverage_from_daily(half)
    assert abs(cov2["tp"] - 0.5) < 1e-9
    assert abs(cov2["coverage"] - 0.5) < 1e-9           # берётся минимум по переменным
    empty = pd.DataFrame({"t2m": np.arange(5, dtype=float)}, index=pd.RangeIndex(5))
    assert coverage_from_daily(empty)["coverage"] == 0.0  # tp нет → ячейка недоступна


def _fake_ds(lats, lons, days=200, bad_cell=(0, 0), frac_nan=0.95, seed=7):
    """Синтетический daily_region: все ячейки полные, кроме bad_cell."""
    import xarray as xr

    rng = np.random.default_rng(seed)
    t = pd.date_range("2000-01-01", periods=days, freq="D")
    shape = (days, len(lats), len(lons))
    t2m = 10 + rng.normal(0, 1, shape)
    tp = np.abs(rng.normal(1, 0.5, shape))
    t2m[:, bad_cell[0], bad_cell[1]][: int(frac_nan * days)] = np.nan
    tp[:, bad_cell[0], bad_cell[1]][: int(frac_nan * days)] = np.nan
    return xr.Dataset(
        {"t2m": (("time", "lat", "lon"), t2m), "tp": (("time", "lat", "lon"), tp)},
        coords={"time": t, "lat": lats, "lon": lons},
    )


def test_build_grid_filters_and_orders():
    lats = [45.75, 45.25]
    lons = [37.25, 37.75, 38.25]
    ds = _fake_ds(lats, lons)                    # битая ячейка — север-запад
    grid = build_grid(None, min_coverage=0.9, cell=0.5, bounds=(45.0, 46.0, 37.0, 38.5), ds=ds)
    assert grid["n_candidates"] == 6
    assert grid["n_cells"] == 5                  # одна ячейка отфильтрована
    pts = grid_points(grid)
    assert [p[0] for p in pts] == ["G01", "G02", "G03", "G04", "G05"]
    # порядок: с севера на юг, внутри строки — с запада на восток
    assert pts[0][1] == 45.75 and pts[0][2] == 37.75   # битая 37.25 пропущена
    assert pts[-1][1] == 45.25 and pts[-1][2] == 38.25
    bad = [c for c in grid["cells"] if c["lat"] == 45.75 and c["lon"] == 37.25]
    assert not bad


def test_artifact_roundtrip(tmp_path):
    p = tmp_path / "krai_grid.json"
    grid = {
        "name": "krai_grid", "cell_deg": 0.5,
        "bounds": dict(zip(("lat_min", "lat_max", "lon_min", "lon_max"), KRA_BOUNDS)),
        "min_coverage": 0.9, "n_candidates": 35, "n_cells": 1,
        "cells": [{"id": "G01", "lat": 45.25, "lon": 38.75,
                   "coverage_t2m": 0.9986, "coverage_tp": 0.9984, "coverage": 0.9984}],
    }
    from agrocast.region.grid import save_grid

    save_grid(grid, p)
    loaded = load_grid(p)
    assert loaded["n_cells"] == 1
    assert grid_points(loaded) == [("G01", 45.25, 38.75)]
    assert json.loads(p.read_text())["cells"][0]["id"] == "G01"


def test_committed_krai_artifact_valid():
    """Зафиксированный артефакт: 28 ячеек КРА с покрытием ≥ 90%."""
    from agrocast.region.grid import DEFAULT_ARTIFACT

    grid = load_grid(DEFAULT_ARTIFACT)
    pts = grid_points(grid)
    assert len(pts) == 28
    assert grid["n_candidates"] == 35
    b = grid["bounds"]
    for pid, la, lo in pts:
        assert b["lat_min"] < la < b["lat_max"]
        assert b["lon_min"] < lo < b["lon_max"]
    covs = [c["coverage"] for c in grid["cells"]]
    assert min(covs) >= grid["min_coverage"]
    assert [p[0] for p in pts] == [f"G{i:02d}" for i in range(1, 29)]
