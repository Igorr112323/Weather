from pathlib import Path
import shutil
import numpy as np
import pandas as pd
import xarray as xr


class ZarrStore:
    def __init__(self, root, shared=None, fallback=None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.shared = Path(shared) if shared else None
        self.fallback = Path(fallback) if fallback else None

    def path(self, name):
        return self.root / name

    def shared_path(self, name):
        paths = [path / name for path in (self.shared, self.fallback) if path is not None]
        return next((path for path in paths if path.exists()), paths[0] if paths else None)

    def exists(self, name):
        if self.path(name).exists():
            return True
        sp = self.shared_path(name)
        return bool(sp and sp.exists())

    def names(self):
        out = {p.name for p in self.root.iterdir() if p.is_dir()}
        for path in (self.shared, self.fallback):
            if path is not None and path.exists():
                out |= {p.name for p in path.iterdir() if p.is_dir()}
        return sorted(out)

    def write(self, name, ds):
        p = self.path(name)
        tmp = p.with_name(p.name + ".tmp")
        if tmp.exists():
            shutil.rmtree(tmp)
        encoding = {}
        for v in ds.data_vars:
            dims = ds[v].dims
            if "time" in dims:
                i = dims.index("time")
                chunks = list(ds[v].shape)
                chunks[i] = min(1024, chunks[i])
                encoding[v] = {"chunks": tuple(chunks)}
        try:
            ds.to_zarr(tmp, mode="w", encoding=encoding if encoding else None, zarr_format=2)
        except TypeError:
            ds.to_zarr(tmp, mode="w", encoding=encoding if encoding else None)
        backup = p.with_name(p.name + ".old")
        if backup.exists():
            shutil.rmtree(backup)
        moved = False
        if p.exists():
            p.rename(backup)
            moved = True
        try:
            tmp.rename(p)
        except OSError:
            if moved and not p.exists() and backup.exists():
                backup.rename(p)
            raise
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
        return p

    def append(self, name, ds_new):
        if not self.exists(name):
            self.write(name, ds_new)
            return
        old = self.open(name)
        combined = xr.concat([old, ds_new], dim="time")
        t = combined.time.values
        _, idx = np.unique(t, return_index=True)
        combined = combined.isel(time=np.sort(idx)).sortby("time")
        self.write(name, combined)

    def open(self, name):
        p = self.path(name)
        if not p.exists():
            sp = self.shared_path(name)
            if sp and sp.exists():
                p = sp
        return xr.open_zarr(p)

    def last_time(self, name):
        if not self.exists(name):
            return None
        ds = self.open(name)
        return pd.Timestamp(ds.time.values[-1])
