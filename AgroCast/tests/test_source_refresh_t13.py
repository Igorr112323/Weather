import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agrocast.ingest.openobs import drop_incomplete_months  # noqa: E402
from agrocast.store.zarrstore import ZarrStore  # noqa: E402


def _daily(idx, tp=None):
    tp = np.full(len(idx), 2.0) if tp is None else tp
    return xr.Dataset(
        {"tp": (("time",), tp), "t2m": (("time",), np.full(len(idx), 18.0))},
        coords={"time": idx},
    )


def test_partial_and_missing_months_table():
    idx = pd.date_range("2026-06-01", "2026-07-20")
    assert len(drop_incomplete_months(_daily(idx)).time) == 30
    tp = np.full(len(idx), 2.0)
    tp[0:10] = np.nan
    assert len(drop_incomplete_months(_daily(idx, tp)).time) == 0
    feb = pd.date_range("2023-02-01", "2023-02-28")
    assert len(drop_incomplete_months(_daily(feb)).time) == 28
    leap = pd.date_range("2024-02-01", "2024-02-29")
    assert len(drop_incomplete_months(_daily(leap)).time) == 29
    allnan = pd.date_range("2026-01-01", "2026-03-31")
    tp3 = np.full(len(allnan), np.nan)
    assert len(drop_incomplete_months(_daily(allnan, tp3)).time) == 0
    sparse = np.full(len(idx), 2.0)
    sparse[44:] = np.nan
    out = drop_incomplete_months(_daily(idx, sparse))
    assert len(out.time) == 30


def test_grid_rows_with_many_nans_reject_month():
    idx = pd.date_range("2026-06-01", "2026-06-30")
    good = xr.Dataset({"tp": (("time", "y", "x"), np.full((30, 2, 2), 1.0))}, coords={"time": idx})
    assert len(drop_incomplete_months(good).time) == 30
    data = np.full((30, 2, 2), 1.0)
    data[0:20, 0, 0] = np.nan
    edge = xr.Dataset({"tp": (("time", "y", "x"), data)}, coords={"time": idx})
    assert len(drop_incomplete_months(edge).time) == 30
    data[0:20, 1, 0] = np.nan
    bad = xr.Dataset({"tp": (("time", "y", "x"), data)}, coords={"time": idx})
    assert len(drop_incomplete_months(bad).time) == 0


def test_append_idempotent_and_merge_safe(tmp_path):
    store = ZarrStore(tmp_path / "zarr")
    idx = pd.date_range("2026-01-01", periods=10)
    first = xr.Dataset({"tp": (("time",), np.arange(10.0))}, coords={"time": idx})
    store.write("daily_region", first)
    store.append("daily_region", first)
    out = store.open("daily_region")
    assert len(out.time) == 10
    assert np.allclose(out["tp"].values, np.arange(10.0))
    more = xr.Dataset({"tp": (("time",), np.arange(10.0, 15.0))}, coords={"time": pd.date_range("2026-01-11", periods=5)})
    store.append("daily_region", more)
    out = store.open("daily_region")
    assert len(out.time) == 15
    assert float(out["tp"].values[-1]) == 14.0


def test_refresh_cli_rejects_busy_lock_and_arg_combos(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    script = ROOT / "scripts" / "refresh_sources.py"
    env = {"PYTHONPATH": str(ROOT), "PATH": "/usr/bin:/bin"}
    res = subprocess.run(
        [sys.executable, str(script), "--state", str(state), "--lat", "45.0"],
        capture_output=True, text=True, env=env, cwd=str(ROOT),
    )
    assert res.returncode == 2
    assert "--lat and --lon" in res.stderr or "required" in res.stderr
    code = f"""
import sys
sys.path.insert(0, {str(ROOT)!r})
from pathlib import Path
from scripts.refresh_sources import acquire_lock
import json
state = Path({str(state)!r})
h1 = acquire_lock(state)
h2 = acquire_lock(state)
print(json.dumps({{"first": h1 is not None, "second": h2 is None}}))
"""
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=str(ROOT))
    assert res.returncode == 0, res.stderr
    assert json.loads(res.stdout) == {"first": True, "second": True}
