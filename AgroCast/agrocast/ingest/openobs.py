import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import xarray as xr

from agrocast.core.config import Config
from agrocast.ingest.registry import Registry
from agrocast.store.zarrstore import ZarrStore

ERSST_URL = "https://www.ncei.noaa.gov/pub/data/cmb/ersst/v5/netcdf/ersst.v5.{ym}.nc"
NCSS = "https://psl.noaa.gov/thredds/ncss/grid"
FILESERVER = "https://psl.noaa.gov/thredds/fileServer"
SLP_FILE = "Datasets/ncep.reanalysis.derived/surface/slp.mon.mean.nc"
HGT_DATASET = "Datasets/ncep.reanalysis.derived/pressure/hgt.mon.mean.nc"
CPC_T = "Datasets/cpc_global_temp/{var}.{year}.nc"
CPC_P = "Datasets/cpc_global_precip/precip.{year}.nc"


def _download(url, target, timeout=600, retries=4):
    import time as _t

    last = None
    for attempt in range(retries):
        try:
            with requests.get(url, stream=True, timeout=timeout) as r:
                r.raise_for_status()
                with open(target, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1 << 18):
                        f.write(chunk)
            return
        except Exception as exc:
            last = exc
            _t.sleep(1.5 * (attempt + 1))
    raise last


def _ncss(dataset, var, north, south, east, west, t0, t1, target, extra="", timeout=240, retries=4):
    import time as _t

    url = (
        f"{NCSS}/{dataset}?var={var}&north={north}&south={south}&east={east}&west={west}"
        f"&accept=netcdf4&time_start={t0}&time_end={t1}{extra}"
    )
    last = None
    for attempt in range(retries):
        _download(url, target, timeout=timeout, retries=2)
        try:
            with xr.open_dataset(target) as ds:
                nt = int(ds.sizes.get("time", 0))
            if nt > 0:
                return xr.open_dataset(target)
            last = ValueError(f"empty time dim in {target}")
        except Exception as exc:
            last = exc
        Path(target).unlink(missing_ok=True)
        _t.sleep(2.0 * (attempt + 1))
    raise last


def _last_complete_period():
    return pd.Period(pd.Timestamp.now(), "M") - 1


def fetch_ersst(config, start_year=1976, end_year=None, workers=8):
    store = ZarrStore(config.zarr_dir)
    reg = Registry(config.registry_path)
    limit = _last_complete_period()
    end_year = end_year or limit.year
    yms = []
    for y in range(start_year, end_year + 1):
        for m in range(1, 13):
            if pd.Period(f"{y}-{m:02d}", "M") <= limit:
                yms.append(f"{y}{m:02d}")
    with tempfile.TemporaryDirectory() as tmp:
        paths = {}
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_download, ERSST_URL.format(ym=ym), str(Path(tmp) / f"{ym}.nc")): ym for ym in yms}
            for f, ym in futs.items():
                try:
                    f.result()
                    paths[ym] = str(Path(tmp) / f"{ym}.nc")
                except Exception:
                    continue
        parts = []
        for ym in sorted(paths):
            try:
                ds = xr.open_dataset(paths[ym], decode_times=False)
                da = ds["sst"]
                if "lev" in da.dims:
                    da = da.isel(lev=0)
                if "time" in da.dims:
                    da = da.isel(time=0, drop=True)
                t = pd.Timestamp(f"{ym[:4]}-{ym[4:]}-15")
                da = da.expand_dims(time=[t]).astype("float32").rename("sst")
                parts.append(da)
            except Exception:
                continue
        sst = xr.concat(parts, dim="time").sortby("time")
    if store.exists("sst"):
        old = store.open("sst")["sst"]
        combined = xr.concat([old, sst], dim="time")
        _, ii = np.unique(combined.time.values, return_index=True)
        sst = combined.isel(time=np.sort(ii)).sortby("time")
    out = sst.to_dataset()
    out = out.drop_vars([v for v in out.coords if v not in ("time", "lat", "lon")], errors="ignore")
    out["sst"].attrs["units"] = "degC"
    store.write("sst", out)
    reg.upsert_file("openobs", "sst", str(store.last_time("sst")))
    reg.log_event("ingest", f"ersst months={len(parts)} last={store.last_time('sst')}")
    return {"months": len(parts), "last": str(store.last_time("sst"))}


def fetch_fields(config):
    store = ZarrStore(config.zarr_dir)
    reg = Registry(config.registry_path)
    limit = _last_complete_period()
    bounds = []
    y = 1948
    while y <= limit.year:
        y2 = min(y + 9, limit.year)
        m2 = limit.month if y2 == limit.year else 12
        bounds.append((f"{y}-01-01T00:00:00Z", f"{y2}-{m2:02d}-28T23:59:59Z"))
        y += 10
    with tempfile.TemporaryDirectory() as tmp:
        slp_parts = []
        hgt_parts = []
        for i, (a, b) in enumerate(bounds):
            slp_parts.append(
                _ncss(
                    "Datasets/ncep.reanalysis.derived/surface/slp.mon.mean.nc", "slp",
                    90, -20, 359.9, 0, a, b, str(Path(tmp) / f"slp{i}.nc"),
                )["slp"].astype("float32")
            )
            hgt_parts.append(
                _ncss(
                    HGT_DATASET, "hgt", 90, -20, 359.9, 0, a, b,
                    str(Path(tmp) / f"hgt{i}.nc"), extra="&vertCoord=500",
                )["hgt"].isel(level=0).astype("float32")
            )
        slp = xr.concat(slp_parts, dim="time")
        hgt = xr.concat(hgt_parts, dim="time")
    _, i1 = np.unique(slp.time.values, return_index=True)
    slp = slp.isel(time=np.sort(i1))
    _, i2 = np.unique(hgt.time.values, return_index=True)
    hgt = hgt.isel(time=np.sort(i2))
    mslp = slp.sortby("lat")
    mslp = mslp.assign_coords(lon=(mslp.lon.values % 360.0)).sortby("lon")
    z500 = hgt.sortby("lat")
    z500 = z500.assign_coords(lon=(z500.lon.values % 360.0)).sortby("lon")
    common = np.intersect1d(mslp.time.values, z500.time.values)
    ds = xr.Dataset(
        {
            "mslp": mslp.sel(time=common).rename("mslp"),
            "z500": z500.sel(time=common).rename("z500"),
        }
    )
    ds["mslp"].attrs["units"] = "hPa"
    ds["z500"].attrs["units"] = "m"
    store.write("fields_monthly", ds)
    reg.upsert_file("openobs", "fields_monthly", str(store.last_time("fields_monthly")))
    reg.log_event("ingest", f"fields last={store.last_time('fields_monthly')}")
    return {"last": str(store.last_time("fields_monthly"))}


def _cpc_url(dataset, var, north, south, east, west, t0, t1):
    return (
        f"{NCSS}/{dataset}?var={var}&north={north}&south={south}&east={east}&west={west}"
        f"&accept=netcdf4&time_start={t0}&time_end={t1}"
    )


def _fetch_cpc_year_files(config, y, tmp):
    r = config.region
    t0 = f"{y}-01-01T00:00:00Z"
    t1 = f"{y}-12-31T23:59:59Z"
    out = {}
    for key, ds_name, var in [
        ("tx", CPC_T.format(var="tmax", year=y), "tmax"),
        ("tn", CPC_T.format(var="tmin", year=y), "tmin"),
        ("pr", CPC_P.format(year=y), "precip"),
    ]:
        out[key] = _download(
            _cpc_url(ds_name, var, r.lat_max, r.lat_min, r.lon_max, r.lon_min, t0, t1),
            str(Path(tmp) / f"{key}{y}.nc"),
            timeout=240,
            retries=4,
        )
    return out


def _open_cpc_year(config, y, tmp):
    try:
        tx = xr.open_dataset(str(Path(tmp) / f"tx{y}.nc"))["tmax"]
        tn = xr.open_dataset(str(Path(tmp) / f"tn{y}.nc"))["tmin"]
        pr = xr.open_dataset(str(Path(tmp) / f"pr{y}.nc"))["precip"]
        if int(tx.sizes.get("time", 0)) < 300:
            raise ValueError(f"cpc {y}: short tmax")
        t2m = ((tx + tn) / 2.0).astype("float32")
        return xr.Dataset({"t2m": t2m.rename("t2m"), "tp": pr.astype("float32").rename("tp")})
    except Exception:
        return None


def fetch_cpc_daily(config, start_year=1979, end_year=None, workers=4):
    store = ZarrStore(config.zarr_dir)
    reg = Registry(config.registry_path)
    limit = _last_complete_period()
    end_year = end_year or limit.year
    years = [y for y in range(start_year, end_year + 1)]
    with tempfile.TemporaryDirectory() as tmp:
        done = set()
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_fetch_cpc_year_files, config, y, tmp): y for y in years}
            for f, y in futs.items():
                try:
                    f.result()
                    done.add(y)
                except Exception as exc:
                    reg.log_event("ingest_warn", f"cpc {y}: {exc}")
        frames = []
        for y in sorted(done):
            ds = _open_cpc_year(config, y, tmp)
            if ds is not None:
                frames.append((y, ds))
        frames.sort(key=lambda t: t[0])
        print("chunk years ok:", [y for y, _ in frames], flush=True)
        if not frames:
            raise RuntimeError(
                "CPC: ни один год не скачался — нет доступа к интернету "
                "(data.rcc-acis.org) или сервис недоступен"
            )
        daily = xr.concat([f for _, f in frames], dim="time").sortby("time")
    daily["t2m"].attrs["units"] = "degC"
    daily["tp"].attrs["units"] = "mm"
    store.append("daily_region", daily)
    reg.upsert_file("openobs", "daily_region", str(store.last_time("daily_region")))
    reg.log_event("ingest", f"cpc daily years={len(frames)} last={store.last_time('daily_region')}")
    return {"years": len(frames), "last": str(store.last_time("daily_region"))}


def fetch_soil(config):
    store = ZarrStore(config.zarr_dir)
    reg = Registry(config.registry_path)
    limit = _last_complete_period()
    bounds = []
    y = 1948
    while y <= limit.year:
        y2 = min(y + 9, limit.year)
        m2 = limit.month if y2 == limit.year else 12
        bounds.append((f"{y}-01-01T00:00:00Z", f"{y2}-{m2:02d}-28T23:59:59Z"))
        y += 10
    r = config.region
    with tempfile.TemporaryDirectory() as tmp:
        parts = []
        for i, (a, b) in enumerate(bounds):
            parts.append(
                _ncss(
                    "Datasets/ncep.reanalysis.derived/surface_gauss/soilw.mon.mean.nc", "soilw",
                    r.lat_max, r.lat_min, r.lon_max, r.lon_min, a, b, str(Path(tmp) / f"sw{i}.nc"),
                )["soilw"].astype("float32")
            )
        soil = xr.concat(parts, dim="time")
    _, ii = np.unique(soil.time.values, return_index=True)
    soil = soil.isel(time=np.sort(ii))
    ds = xr.Dataset({"swvl": soil.sortby(["lat", "lon"]).rename("swvl")})
    ds["swvl"].attrs["units"] = "fraction"
    store.write("soil_monthly", ds)
    reg.upsert_file("openobs", "soil_monthly", str(store.last_time("soil_monthly")))
    reg.log_event("ingest", f"soil last={store.last_time('soil_monthly')}")
    return {"last": str(store.last_time("soil_monthly"))}


OISST_MON = "Datasets/noaa.oisst.v2.highres/sst.mon.mean.nc"


def fetch_oisst_boxes(config, start_year=1981):
    store = ZarrStore(config.zarr_dir)
    reg = Registry(config.registry_path)
    limit = _last_complete_period()
    bounds = []
    y = start_year
    while y <= limit.year:
        y2 = min(y + 9, limit.year)
        m2 = limit.month if y2 == limit.year else 12
        bounds.append((f"{y}-01-01T00:00:00Z", f"{y2}-{m2:02d}-28T23:59:59Z"))
        y += 10
    with tempfile.TemporaryDirectory() as tmp:
        parts = []
        for i, (a, b) in enumerate(bounds):
            parts.append(
                _ncss(OISST_MON, "sst", 48, 27, 45, 0, a, b, str(Path(tmp) / f"oi{i}.nc"), timeout=300, retries=4)["sst"].astype("float32")
            )
        sst = xr.concat(parts, dim="time").sortby("time")
    _, ii = np.unique(sst.time.values, return_index=True)
    sst = sst.isel(time=np.sort(ii)).sortby("lat")
    sst = sst.assign_coords(lon=(sst.lon.values % 360.0)).sortby("lon")

    def box_mean(da, lat0, lat1, lon0, lon1):
        sub = da.sel(lat=slice(lat0, lat1), lon=slice(lon0, lon1))
        w = np.sqrt(np.clip(np.cos(np.radians(sub.lat.values)), 0.0, 1.0))
        x = sub.values.astype(float)
        ok = np.isfinite(x)
        num = (np.where(ok, x, 0.0) * w[None, :, None]).sum(axis=(1, 2))
        den = (ok * w[None, :, None]).sum(axis=(1, 2))
        out = np.full(x.shape[0], np.nan)
        m = den > 0
        out[m] = num[m] / den[m]
        idx = pd.PeriodIndex(pd.DatetimeIndex(sub.time.values), freq="M")
        return pd.Series(out, index=idx)

    med = box_mean(sst, 30, 45, 0, 40)
    black = box_mean(sst, 41, 47, 27, 41)
    ds = xr.Dataset(
        {
            "med_sst": (("time",), med.to_numpy("float32")),
            "black_sst": (("time",), black.to_numpy("float32")),
        },
        coords={"time": med.index.to_timestamp()},
    )
    store.write("oisst_boxes", ds)
    reg.upsert_file("openobs", "oisst_boxes", str(store.last_time("oisst_boxes")))
    reg.log_event("ingest", f"oisst boxes months={len(med)} last={store.last_time('oisst_boxes')}")
    return {"months": int(len(med)), "last": str(store.last_time("oisst_boxes"))}


def fetch_strat_snow(config, start_year=1948):
    store = ZarrStore(config.zarr_dir)
    reg = Registry(config.registry_path)
    limit = _last_complete_period()
    bounds = []
    y = start_year
    while y <= limit.year:
        y2 = min(y + 9, limit.year)
        m2 = limit.month if y2 == limit.year else 12
        bounds.append((f"{y}-01-01T00:00:00Z", f"{y2}-{m2:02d}-28T23:59:59Z"))
        y += 10
    with tempfile.TemporaryDirectory() as tmp:
        uparts, zparts, sparts = [], [], []
        for i, (a, b) in enumerate(bounds):
            uparts.append(_ncss("Datasets/ncep.reanalysis.derived/pressure/uwnd.mon.mean.nc", "uwnd", 70, 55, 359.9, 0, a, b, str(Path(tmp) / f"u{i}.nc"), extra="&vertCoord=10")["uwnd"].isel(level=0).astype("float32"))
            zparts.append(_ncss("Datasets/ncep.reanalysis.derived/pressure/hgt.mon.mean.nc", "hgt", 90, 60, 359.9, 0, a, b, str(Path(tmp) / f"z{i}.nc"), extra="&vertCoord=50")["hgt"].isel(level=0).astype("float32"))
            sparts.append(_ncss("Datasets/ncep.reanalysis.derived/surface_gauss/weasd.sfc.mon.mean.nc", "weasd", 65, 45, 140, 20, a, b, str(Path(tmp) / f"s{i}.nc"))["weasd"].astype("float32"))
        u = xr.concat(uparts, dim="time").sortby("time")
        z = xr.concat(zparts, dim="time").sortby("time")
        sn = xr.concat(sparts, dim="time").sortby("time")

    def series(da):
        _, ii = np.unique(da.time.values, return_index=True)
        da = da.isel(time=np.sort(ii))
        w = np.sqrt(np.clip(np.cos(np.radians(da.lat.values)), 0.0, 1.0))
        x = da.values.astype(float)
        ok = np.isfinite(x)
        num = (np.where(ok, x, 0.0) * w[None, :, None]).sum(axis=(1, 2))
        den = (ok * w[None, :, None]).sum(axis=(1, 2))
        out = np.full(x.shape[0], np.nan)
        m = den > 0
        out[m] = num[m] / den[m]
        idx = pd.PeriodIndex(pd.DatetimeIndex(da.time.values), freq="M")
        return pd.Series(out, index=idx)

    su, sz, ss = series(u), series(z), series(sn)
    common = su.index.intersection(sz.index).intersection(ss.index)
    ds = xr.Dataset(
        {
            "u10": (("time",), su.reindex(common).to_numpy("float32")),
            "z50": (("time",), sz.reindex(common).to_numpy("float32")),
            "snow_eur": (("time",), ss.reindex(common).to_numpy("float32")),
        },
        coords={"time": common.to_timestamp()},
    )
    store.write("strat_snow", ds)
    reg.upsert_file("openobs", "strat_snow", str(store.last_time("strat_snow")))
    reg.log_event("ingest", f"strat_snow months={len(common)} last={store.last_time('strat_snow')}")
    return {"months": int(len(common)), "last": str(store.last_time("strat_snow"))}


def update_all(config, sst_start=1976, cpc_start=1979):
    out = {}
    out["sst"] = fetch_ersst(config, start_year=sst_start)
    out["fields"] = fetch_fields(config)
    out["daily"] = fetch_cpc_daily(config, start_year=cpc_start)
    try:
        out["soil"] = fetch_soil(config)
    except Exception as exc:
        out["soil"] = {"error": str(exc)[:200]}
    return out
