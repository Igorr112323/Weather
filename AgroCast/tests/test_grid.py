import json

import numpy as np
import pandas as pd

from agrocast.region.grid import (
    KRA_BOUNDS,
    cell_centers,
    coverage_from_daily,
    krai_cells,
    save_grid,
    load_grid,
    grid_points,
)


class FakeStore:
    def __init__(self, ds):
        self._ds = ds

    def open(self, name):
        return self._ds


def test_centers_in_bounds_and_count():
    lats, lons = cell_centers()
    lat_min, lat_max, lon_min, lon_max = KRA_BOUNDS
    assert len(lats) * len(lons) == 35
    assert (lats > lat_min).all() and (lats < lat_max).all()
    assert (lons > lon_min).all() and (lons < lon_max).all()
    assert np.allclose(np.diff(lats), 0.5) and np.allclose(np.diff(lons), 0.5)
    assert abs(lats[0] - (lat_min + 0.25)) < 1e-9
    assert abs(lons[0] - (lon_min + 0.25)) < 1e-9


def test_coverage_from_daily():
    idx = pd.period_range("2000-01", "2000-12", freq="D").to_timestamp()
    full = pd.DataFrame({"t2m": np.arange(len(idx), dtype=float), "tp": 1.0}, index=idx)
    cov = coverage_from_daily(full)
    assert cov["t2m"] == 1.0 and cov["tp"] == 1.0 and cov["coverage"] == 1.0
    half = full.copy()
    half.loc[idx[: len(idx) // 2], "tp"] = np.nan
    cov2 = coverage_from_daily(half)
    assert abs(cov2["tp"] - 0.5) < 1e-9
    assert abs(cov2["coverage"] - 0.5) < 1e-9
    empty = pd.DataFrame({"t2m": np.arange(5, dtype=float)}, index=pd.RangeIndex(5))
    assert coverage_from_daily(empty)["coverage"] == 0.0


def _fake_ds(lats, lons, days=200, bad_cell=(0, 0), frac_nan=0.95, seed=7):
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


def test_krai_cells_filters_and_orders():
    lats = [45.75, 45.25]
    lons = [37.25, 37.75, 38.25]
    store = FakeStore(_fake_ds(lats, lons))
    cells = krai_cells(store, min_coverage=0.9, cell=0.5, bounds=(45.0, 46.0, 37.0, 38.5))
    assert len(cells) == 5
    assert [c["id"] for c in cells] == ["P01", "P02", "P03", "P04", "P05"]
    assert cells[0]["lat"] == 45.75 and cells[0]["lon"] == 37.75
    assert cells[-1]["lat"] == 45.25 and cells[-1]["lon"] == 38.25
    assert not [c for c in cells if c["lat"] == 45.75 and c["lon"] == 37.25]
    assert min(c["coverage"] for c in cells) >= 0.9


def test_artifact_roundtrip(tmp_path):
    p = tmp_path / "krai_grid.json"
    cells = [{"id": "P01", "lat": 45.25, "lon": 38.75,
              "coverage_t2m": 0.9986, "coverage_tp": 0.9984, "coverage": 0.9984}]
    grid = save_grid(cells, path=p)
    assert grid["n_candidates"] == 35 and grid["n_cells"] == 1
    loaded = load_grid(p)
    assert loaded["n_cells"] == 1
    assert grid_points(loaded) == [("P01", 45.25, 38.75)]
    assert json.loads(p.read_text())["cells"][0]["id"] == "P01"


def test_committed_krai_artifact_valid():
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
    assert [p[0] for p in pts] == [f"P{i:02d}" for i in range(1, 29)]
