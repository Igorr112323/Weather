from pathlib import Path
import time
import requests
import xarray as xr

from agrocast.core.config import Config
from agrocast.ingest.registry import Registry
from agrocast.store.zarrstore import ZarrStore

ERSST_URL = "https://psl.noaa.gov/thredds/fileServer/Datasets/ersstv5/sst.mnmean.nc"


def _download(url, target, timeout=600):
    with requests.get(url, stream=True, timeout=timeout) as r:
        r.raise_for_status()
        with open(target, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)


def _file_age_days(path):
    return (time.time() - Path(path).stat().st_mtime) / 86400.0


def update(config, max_age_days=30.0):
    store = ZarrStore(config.zarr_dir)
    reg = Registry(config.registry_path)
    target = Path(config.raw_dir) / "ersstv5.mnmean.nc"
    need = (not target.exists()) or (_file_age_days(target) > max_age_days)
    if not need:
        reg.upsert_file("ersst", "sst", str(store.last_time("sst")) if store.exists("sst") else None)
        return {"updated": False}
    _download(ERSST_URL, target)
    ds = xr.open_dataset(target)
    sst = ds["sst"].astype("float32").sortby("lat")
    sst = sst.assign_coords(lon=(ds["lon"].values % 360.0)).sortby("lon")
    out = sst.rename("sst").to_dataset()
    out = out.drop_vars([v for v in out.coords if v not in ("time", "lat", "lon")], errors="ignore")
    out["sst"].attrs["units"] = "degC"
    store.write("sst", out)
    reg.upsert_file("ersst", "sst", str(store.last_time("sst")))
    reg.log_event("ingest", f"ersst updated last={store.last_time('sst')}")
    return {"updated": True, "last_time": str(store.last_time("sst"))}
