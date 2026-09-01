import numpy as np
import pandas as pd
import xarray as xr

from agrocast.core.config import Config
from agrocast.ingest.registry import Registry
from agrocast.store.zarrstore import ZarrStore


def _ar1(n, rho, sd, rng):
    x = np.zeros(n)
    a = 0.0
    for i in range(n):
        a = rho * a + sd * rng.standard_normal()
        x[i] = a
    return x


def _signals(n, rng):
    return {
        "enso": _ar1(n, 0.92, 0.45, rng),
        "pdo": _ar1(n, 0.95, 0.35, rng),
        "amo": _ar1(n, 0.985, 0.25, rng),
        "nao": _ar1(n, 0.75, 1.0, rng),
        "ao": _ar1(n, 0.8, 1.0, rng),
        "scand": _ar1(n, 0.7, 1.0, rng),
        "pol": _ar1(n, 0.7, 1.0, rng),
    }


def make_sst(signals, months, rng):
    t = months.to_timestamp()
    lat = np.arange(-70.0, 70.01, 2.5)
    lon = np.arange(0.0, 360.0, 2.5)
    La = lat[:, None]
    Ld = lon[None, :]
    base = 29.0 * np.exp(-((La / 23.0) ** 2)) + 18.0 * np.exp(-(((np.abs(La) - 38.0) / 18.0) ** 2))
    p_enso = np.exp(-((La / 9.0) ** 2)) * np.exp(-(((Ld - 225.0) / 35.0) ** 2))
    p_pdo = np.exp(-(((La - 45.0) / 10.0) ** 2)) * np.exp(-(((Ld - 190.0) / 25.0) ** 2)) - 0.6 * np.exp(-(((La - 30.0) / 10.0) ** 2)) * np.exp(-(((Ld - 185.0) / 30.0) ** 2))
    p_amo = np.exp(-(((La - 32.0) / 18.0) ** 2)) * np.exp(-(((Ld - 320.0) / 28.0) ** 2))
    n = len(t)
    yr = (t.year - t.year[0]).to_numpy()[:, None, None].astype(float)
    trend = 0.015 * yr * (0.5 + 0.5 * np.exp(-((La[None, :, :] / 40.0) ** 2)))
    noise = rng.standard_normal((n, len(lat), len(lon))) * (0.45 - 0.22 * np.exp(-((La[None, :, :] / 25.0) ** 2)))
    sst = (
        base[None, :, :]
        + p_enso[None, :, :] * signals["enso"][:, None, None]
        + p_pdo[None, :, :] * signals["pdo"][:, None, None]
        + p_amo[None, :, :] * signals["amo"][:, None, None]
        + trend
        + noise
    )
    ds = xr.Dataset(
        {"sst": (("time", "lat", "lon"), sst.astype(np.float32))},
        coords={"time": t, "lat": lat, "lon": lon},
    )
    ds["sst"].attrs["units"] = "degC"
    return ds


def make_fields(signals, months, rng):
    t = months.to_timestamp()
    lat = np.arange(-45.0, 86.01, 2.5)
    lon = np.arange(0.0, 360.0, 2.5)
    La = lat[:, None]
    Ld = lon[None, :]
    n = len(t)
    mm = months.month.to_numpy()
    seas = np.sin(2 * np.pi * (mm - 1) / 12.0)[:, None, None]
    p_nao = np.exp(-(((La - 37.0) / 5.0) ** 2)) * np.exp(-(((Ld - 335.0) / 12.0) ** 2)) - np.exp(-(((La - 65.0) / 6.0) ** 2)) * np.exp(-(((Ld - 340.0) / 12.0) ** 2))
    p_ao = np.exp(-(((La - 80.0) / 8.0) ** 2)) * np.ones_like(Ld)
    p_scand = np.exp(-(((La - 62.0) / 6.0) ** 2)) * np.exp(-(((Ld - 40.0) / 15.0) ** 2))
    p_pol = np.exp(-(((La - 70.0) / 7.0) ** 2)) * np.exp(-(((Ld - 75.0) / 18.0) ** 2))
    zbase = 54000.0 - 70.0 * (La - 30.0)
    mslp = (
        1013.0
        + 2.0 * seas * np.sign(La[None, :, :])
        + p_nao[None, :, :] * signals["nao"][:, None, None]
        + p_ao[None, :, :] * signals["ao"][:, None, None] * 0.8
        + rng.standard_normal((n, len(lat), len(lon))) * 1.0
    )
    z500 = (
        zbase[None, :, :]
        + 250.0 * seas
        + p_scand[None, :, :] * 250.0 * signals["scand"][:, None, None]
        + p_pol[None, :, :] * 200.0 * signals["pol"][:, None, None]
        + p_nao[None, :, :] * 120.0 * signals["nao"][:, None, None]
        + rng.standard_normal((n, len(lat), len(lon))) * 120.0
    )
    ds = xr.Dataset(
        {
            "mslp": (("time", "lat", "lon"), mslp.astype(np.float32)),
            "z500": (("time", "lat", "lon"), z500.astype(np.float32)),
        },
        coords={"time": t, "lat": lat, "lon": lon},
    )
    ds["mslp"].attrs["units"] = "hPa"
    ds["z500"].attrs["units"] = "m2/s2"
    return ds


def make_daily(signals, months, region, grid, rng):
    lats = np.arange(region.lat_min, region.lat_max + 1e-6, grid)
    lons = np.arange(region.lon_min, region.lon_max + 1e-6, grid)
    dates = pd.date_range(months[0].to_timestamp(), months[-1].to_timestamp(how="end").normalize(), freq="D")
    mper = pd.PeriodIndex(dates, freq="M")
    enso_s = pd.Series(signals["enso"], index=months).reindex(mper).to_numpy()
    amo_s = pd.Series(signals["amo"], index=months).reindex(mper).to_numpy()
    doy = dates.dayofyear.to_numpy()
    yrs = dates.year.to_numpy()
    mth = dates.month.to_numpy()
    nlat, nlon = len(lats), len(lons)
    cells = nlat * nlon
    winter = np.isin(mth, [11, 12, 1, 2, 3])
    autumn = np.isin(mth, [9, 10, 11])
    latc = np.repeat(lats, nlon)
    lonc = np.tile(lons, nlat)
    mu_lat = 11.0 - 0.8 * (latc - 45.0) - 6.0 * np.maximum(lonc - 42.0, 0.0) * 0.0
    t2m_l, tp_l, swvl_l, snow_l = [], [], [], []
    e = np.zeros(cells)
    soil = np.full(cells, 0.30)
    wet = rng.random(cells) < 0.3
    trend_t = 0.025 * (ytrs := (yrs - 2000).astype(float))
    for i in range(len(dates)):
        doy_i, y_i = doy[i], yrs[i]
        mu = mu_lat - 13.2 * np.cos(2 * np.pi * (doy_i - 15) / 365.0) + 0.025 * (y_i - 2000)
        e = 0.75 * e + 3.2 * rng.standard_normal(cells)
        warm_bias = -8.0 * (soil - 0.30)
        enso_eff = 1.2 * enso_s[i] * winter[i]
        t2m = mu + enso_eff + warm_bias + e
        p_wet = 0.28 + 0.10 * np.sin(2 * np.pi * (doy_i - 130) / 365.0)
        p_wet *= 1.0 - 0.0015 * (y_i - 1980)
        p = p_wet * np.where(wet, 1.6, 0.75)
        p = np.clip(p, 0.02, 0.98)
        wet = rng.random(cells) < p
        amount = rng.gamma(0.9, 9.0, cells) * wet
        amount = amount * (1.0 + 0.25 * amo_s[i] * autumn[i])
        tp = amount
        soil = np.clip(0.88 * soil + 0.0009 * (tp - 6.0) + 0.0005 * rng.standard_normal(cells), 0.05, 0.5)
        snow = np.where(t2m < 0.0, np.maximum(-t2m * 0.004 + 0.002 * rng.standard_normal(cells), 0.0), 0.0)
        t2m_l.append(t2m.astype(np.float32))
        tp_l.append(tp.astype(np.float32))
        swvl_l.append(soil.astype(np.float32))
        snow_l.append(snow.astype(np.float32))
    def stack(lst):
        a = np.stack(lst)
        return a.reshape(len(dates), nlat, nlon)
    ds = xr.Dataset(
        {
            "t2m": (("time", "lat", "lon"), stack(t2m_l)),
            "tp": (("time", "lat", "lon"), stack(tp_l)),
            "swvl": (("time", "lat", "lon"), stack(swvl_l)),
            "snow": (("time", "lat", "lon"), stack(snow_l)),
        },
        coords={"time": dates, "lat": lats, "lon": lons},
    )
    ds["t2m"].attrs["units"] = "degC"
    ds["tp"].attrs["units"] = "mm"
    ds["swvl"].attrs["units"] = "m3/m3"
    ds["snow"].attrs["units"] = "m"
    return ds


def build_synthetic(config, end=None):
    rng = np.random.default_rng(config.random_state)
    end_period = pd.Period(pd.Timestamp(end) if end else pd.Timestamp.now(), "M")
    months = pd.period_range(f"{config.train_start}-01", end_period, freq="M")
    signals = _signals(len(months), rng)
    store = ZarrStore(config.zarr_dir)
    reg = Registry(config.registry_path)
    sst = make_sst(signals, months, rng)
    fields = make_fields(signals, months, rng)
    daily = make_daily(signals, months, config.region, config.daily_grid, rng)
    store.write("sst", sst)
    store.write("fields_monthly", fields)
    store.write("daily_region", daily)
    for name in ["sst", "fields_monthly"]:
        reg.upsert_file("synthetic", name, str(store.last_time(name)))
    reg.upsert_file("synthetic", "daily_region", str(store.last_time("daily_region")))
    reg.log_event("ingest", "synthetic datasets rebuilt")
    return {"sst": "sst", "fields": "fields_monthly", "daily": "daily_region"}
