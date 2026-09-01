import argparse
import re
import tempfile
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

from agrocast.ingest.registry import Registry
from agrocast.store.zarrstore import ZarrStore

BUCKET = "noaa-cdr-snow-cover-ext-north-pds"
BASE = f"https://{BUCKET}.s3.amazonaws.com/"
NA = -127
RADIUS_DEG = 1.2


def latest_key():
    with urllib.request.urlopen(BASE + "?list-type=2&prefix=data/", timeout=60) as r:
        body = r.read().decode()
    keys = re.findall(r"<Key>(data/[^<]+\.nc)</Key>", body)
    if not keys:
        raise RuntimeError("в открытом ведре NOAA CDR нет файлов снегового покрытия")
    return sorted(keys)[-1]


def download(path=None):
    path = Path(path) if path else Path(tempfile.gettempdir()) / "nhsce.nc"
    if not path.exists() or path.stat().st_size < 1_000_000:
        key = latest_key()
        path.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(BASE + key, path)
    return path


def _cell_masks(ds, targets, radius_deg=RADIUS_DEG):
    lat = ds["latitude"].values
    lon = ds["longitude"].values % 360.0
    land = ds["land"].values == 1
    return [
        land & (np.abs(lat - tlat) <= radius_deg) & (np.abs(((lon - tlon % 360.0 + 180) % 360) - 180) <= radius_deg)
        for tlat, tlon in targets
    ]


def monthly_fraction(ds, targets, radius_deg=RADIUS_DEG):
    masks = _cell_masks(ds, targets, radius_deg)
    vals = ds["snow_cover_extent"].values.astype(np.float32)
    vals[vals == NA] = np.nan
    times = pd.DatetimeIndex(ds["time"].values)
    per = pd.PeriodIndex(times, freq="M")
    cols = {}
    for k, m in enumerate(masks):
        if m.sum() == 0:
            cols[k] = np.full(len(per), np.nan)
        else:
            cols[k] = np.nanmean(vals[:, m], axis=1)
    df = pd.DataFrame(cols, index=per)
    return df.groupby(level=0).mean()


def update(config, nc_path=None):
    store = ZarrStore(config.zarr_dir)
    if not store.exists("soil_monthly"):
        raise RuntimeError("в мире нет soil_monthly — сначала базовая почва")
    reg = Registry(config.registry_path)
    ds = xr.open_dataset(download(nc_path))
    old = store.open("soil_monthly")
    lats = [float(v) for v in old["lat"].values]
    lons = [float(v) for v in old["lon"].values]
    targets = [(la, lo) for la in lats for lo in lons]
    frac = monthly_fraction(ds, targets)
    idx_old = pd.PeriodIndex(old["time"].values, freq="M")
    full = frac.reindex(frac.index.union(idx_old)).sort_index()
    grids = np.full((len(full.index), len(lats), len(lons)), np.nan, np.float32)
    for i, la in enumerate(lats):
        for j, lo in enumerate(lons):
            k = i * len(lons) + j
            if k in frac.columns:
                grids[:, i, j] = frac[k].reindex(full.index).to_numpy(np.float32)
    times = full.index.to_timestamp()
    snow = xr.DataArray(grids, dims=["time", "lat", "lon"], coords={"time": times, "lat": lats, "lon": lons}, name="snow")
    snow.attrs["units"] = "fraction"
    snow.attrs["source"] = BUCKET
    merged = xr.Dataset({"swvl": old["swvl"].reindex(time=times), "snow": snow})
    store.write("soil_monthly", merged)
    reg.log_event("ingest", f"snowcdr last={full.index.max()}")
    return {"last": str(full.index.max()), "cells": len(targets)}


def main(argv=None):
    ap = argparse.ArgumentParser(description="NOAA CDR снеговое покрытие → почвенный zarr мира")
    ap.add_argument("--world", default=str(Path(__file__).resolve().parent.parent.parent / "world"))
    ap.add_argument("--file", default=None)
    args = ap.parse_args(argv)
    from agrocast.core.config import Config
    from agrocast.serve.pipeline import world_config

    cfg = world_config(args.world) if Path(args.world, "config.json").exists() else Config(data_dir=args.world)
    print(update(cfg, args.file))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
