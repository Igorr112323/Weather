import tempfile
from pathlib import Path
import numpy as np
import pandas as pd
import xarray as xr

try:
    import cdsapi
except ImportError:
    cdsapi = None

from agrocast.core.config import Config
from agrocast.ingest.registry import Registry
from agrocast.store.zarrstore import ZarrStore

DAILY_MEAN_VARS = ["2m_temperature", "volumetric_soil_water_layer_1", "snow_depth_water_equivalent"]
DAILY_SUM_VARS = ["total_precipitation_sum"]
RENAME_DAILY = {
    "t2m": "t2m",
    "tp": "tp",
    "swvl1": "swvl",
    "sd": "snow",
}


def _client():
    if cdsapi is None:
        raise RuntimeError("cdsapi is not installed")
    return cdsapi.Client()


def _to_0360(ds):
    ds = ds.assign_coords(lon=(ds.lon.values % 360.0))
    return ds.sortby("lon").sortby("lat")


def _open_nc(path):
    ds = xr.open_dataset(path)
    return _to_0360(ds)


def _fetch(request, target):
    c = _client()
    c.retrieve(request.pop("dataset"), request, str(target))
    return _open_nc(target)


def download_daily_year(config, year):
    r = config.region
    area = [r.lat_max, r.lon_min, r.lat_min, r.lon_max]
    months = [f"{m:02d}" for m in range(1, 13)]
    days = [f"{d:02d}" for d in range(1, 32)]
    parts = []
    with tempfile.TemporaryDirectory() as tmp:
        for stats, variables in [("daily_mean", DAILY_MEAN_VARS), ("daily_sum", DAILY_SUM_VARS)]:
            target = Path(tmp) / f"daily_{stats}.nc"
            req = {
                "dataset": "reanalysis-era5-land-daily-stats",
                "variable": variables,
                "year": str(year),
                "month": months,
                "day": days,
                "frequency": "daily",
                "statistics": stats,
                "data_format": "netcdf",
                "download_format": "unarchived",
                "area": area,
            }
            parts.append(_fetch(req, target))
    ds = xr.merge(parts)
    ren = {k: v for k, v in RENAME_DAILY.items() if k in ds.variables}
    ds = ds.rename(ren)
    out = {}
    if "t2m" in ds:
        out["t2m"] = ds["t2m"] - 273.15
    if "tp" in ds:
        out["tp"] = ds["tp"] * 1000.0
    if "swvl" in ds:
        out["swvl"] = ds["swvl"]
    if "snow" in ds:
        out["snow"] = ds["snow"]
    new = xr.Dataset({k: (v.dims, v.astype("float32")) for k, v in out.items()}, coords=ds.coords)
    new = new.drop_vars([v for v in new.coords if v not in ("time", "lat", "lon")], errors="ignore")
    return new


def download_fields_year(config, year):
    months = [f"{m:02d}" for m in range(1, 13)]
    area = [85.0, -80.0, 20.0, 60.0]
    parts = []
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "mslp.nc"
        req = {
            "dataset": "reanalysis-era5-single-levels-monthly-means",
            "product_type": "monthly_averaged_reanalysis",
            "variable": ["mean_sea_level_pressure"],
            "year": str(year),
            "month": months,
            "time": "00:00",
            "data_format": "netcdf",
            "download_format": "unarchived",
            "area": area,
        }
        parts.append(_fetch(req, target).rename({"msl": "mslp"}))
        target = Path(tmp) / "z500.nc"
        req = {
            "dataset": "reanalysis-era5-pressure-levels-monthly-means",
            "product_type": "monthly_averaged_reanalysis",
            "variable": ["geopotential"],
            "pressure_level": "500",
            "year": str(year),
            "month": months,
            "time": "00:00",
            "data_format": "netcdf",
            "download_format": "unarchived",
            "area": area,
        }
        parts.append(_fetch(req, target).rename({"z": "z500"}))
    ds = xr.merge(parts)[["mslp", "z500"]]
    new = xr.Dataset(
        {k: (ds[k].dims, ds[k].astype("float32")) for k in ["mslp", "z500"]},
        coords=ds.coords,
    )
    new = new.drop_vars([v for v in new.coords if v not in ("time", "lat", "lon")], errors="ignore")
    return new


def update(config, start_year=None, end_year=None):
    store = ZarrStore(config.zarr_dir)
    reg = Registry(config.registry_path)
    end_year = end_year or pd.Timestamp.now().year
    start_year = start_year or config.train_start
    done = []
    for year in range(start_year, end_year + 1):
        try:
            daily = download_daily_year(config, year)
            store.append("daily_region", daily)
            reg.upsert_file("era5", "daily_region", str(store.last_time("daily_region")))
            fields = download_fields_year(config, year)
            store.append("fields_monthly", fields)
            reg.upsert_file("era5", "fields_monthly", str(store.last_time("fields_monthly")))
            done.append(year)
        except Exception as exc:
            reg.upsert_file("era5", f"fail_{year}", str(pd.Timestamp.now()), status=str(exc)[:200])
            reg.log_event("ingest_fail", f"{year}: {exc}")
    reg.log_event("ingest", f"era5 years done={done[-3:] if done else []}")
    return done
