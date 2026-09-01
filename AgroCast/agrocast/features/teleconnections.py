import numpy as np
import pandas as pd
import xarray as xr

from agrocast.core.geo import cos_lat

IDX_COLS = ["nino34", "soi", "nao", "ao", "scand", "pol", "pdo", "amo", "iod"]


def _prep(da):
    da = da.sortby("lat")
    da = da.assign_coords(lon=(da.lon.values % 360.0)).sortby("lon")
    return da


def _box_series(da, lat0, lat1, lon0, lon1):
    sub = da.sel(lat=slice(lat0, lat1), lon=slice(lon0, lon1))
    if sub.sizes.get("time", 0) == 0 or sub.sizes.get("lat", 0) == 0 or sub.sizes.get("lon", 0) == 0:
        return None
    vals = np.asarray(sub.values, float)
    w = cos_lat(sub.lat.values)
    m = np.isfinite(vals)
    wm = w[None, :, None] * m
    num = np.where(m, vals, 0.0)
    num = (num * w[None, :, None]).sum(axis=(1, 2))
    den = wm.sum(axis=(1, 2))
    ok = den > 0
    y = np.full(vals.shape[0], np.nan)
    y[ok] = num[ok] / den[ok]
    idx = pd.PeriodIndex(pd.DatetimeIndex(sub.time.values), freq="M")
    return pd.Series(y, index=idx)


def _anom(da, base):
    b = da.sel(time=slice(base[0], base[1]))
    clim = b.groupby("time.month").mean("time")
    return da.groupby("time.month") - clim


def _std(s, base):
    y0 = pd.Period(base[0], "M").year
    y1 = pd.Period(base[1], "M").year
    b = s[(s.index.year >= y0) & (s.index.year <= y1)]
    if len(b) < 10:
        b = s.dropna()
    mu, sd = b.mean(), b.std()
    if not np.isfinite(sd) or sd < 1e-9:
        sd = 1.0
    return (s - mu) / sd


def compute_indices(sst_ds, fields_ds, base=("1991-01-01", "2020-12-31")):
    sst = _prep(sst_ds["sst"])
    mslp = _prep(fields_ds["mslp"])
    z500 = _prep(fields_ds["z500"])
    sstA = _anom(sst, base)
    mslpA = _anom(mslp, base)
    zA = _anom(z500, base)
    tropics = _box_series(sstA, -30, 30, 0, 360)
    cols = {}
    cols["nino34"] = _box_series(sstA, -5, 5, 190, 240)
    cols["pdo"] = _box_series(sstA, 20, 60, 150, 220) - (tropics if tropics is not None else 0.0)
    cols["amo"] = _box_series(sstA, 0, 65, 280, 360) - (tropics if tropics is not None else 0.0)
    cols["iod"] = _box_series(sstA, -10, 5, 50, 70) - _box_series(sstA, -10, 0, 90, 110)
    tahiti = _box_series(mslpA, -20, -15, 209, 213)
    darwin = _box_series(mslpA, -15, -10, 129, 132)
    if tahiti is not None and darwin is not None:
        cols["soi"] = tahiti - darwin
    azores = _box_series(mslpA, 35, 40, 328, 336)
    iceland = _box_series(mslpA, 63, 68, 336, 345)
    if azores is not None and iceland is not None:
        cols["nao"] = azores - iceland
    cols["ao"] = _box_series(mslpA, 70, 90, 0, 360) * -1.0
    cols["scand"] = _box_series(zA, 55, 70, 20, 60)
    cols["pol"] = _box_series(zA, 60, 80, 40, 110)
    df = pd.DataFrame({k: v for k, v in cols.items() if v is not None})
    df = df.dropna(how="all")
    return df


def indices_from_store(config, store):
    sst = store.open("sst")
    fields = store.open("fields_monthly")
    base = (f"{config.base_start}-01-01", f"{config.base_end}-12-31")
    return compute_indices(sst, fields, base=base)
