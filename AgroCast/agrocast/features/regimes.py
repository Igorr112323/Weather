import numpy as np
import pandas as pd
from sklearn.cluster import KMeans

from agrocast.store.zarrstore import ZarrStore

NAE_LAT = (30.0, 80.0)
NAE_LON_WEST = (300.0, 360.0)
NAE_LON_EAST = (0.0, 40.0)
N_PC = 4
K_REGIMES = 4
SEED = 0


def compute_regimes(config):
    store = ZarrStore(config.zarr_dir)
    z = store.open("fields_monthly")["z500"].sortby("lat")
    lat = z.lat.values
    lon = z.lon.values % 360.0
    z = z.assign_coords(lon=lon).sortby("lon")
    m_lat = (lat >= NAE_LAT[0]) & (lat <= NAE_LAT[1])
    m_lon = ((lon >= NAE_LON_WEST[0]) & (lon < NAE_LON_WEST[1])) | ((lon >= NAE_LON_EAST[0]) & (lon <= NAE_LON_EAST[1]))
    nae = z.isel(lat=np.where(m_lat)[0], lon=np.where(m_lon)[0])
    idx = pd.PeriodIndex(pd.DatetimeIndex(nae.time.values), freq="M")
    base_mask = (idx.year >= config.base_start) & (idx.year <= config.base_end)
    clim = nae.isel(time=np.where(base_mask)[0]).groupby("time.month").mean("time")
    anom = nae.groupby("time.month") - clim
    w = np.sqrt(np.clip(np.cos(np.radians(anom.lat.values)), 0.0, 1.0))
    Xr = anom.values.astype(float) * w[None, :, None]
    T = Xr.shape[0]
    flat = Xr.reshape(T, -1)
    ok = np.isfinite(flat).all(axis=0)
    F = flat[:, ok]
    Fb = F[base_mask]
    Fc = Fb - Fb.mean(axis=0, keepdims=True)
    U, S, Vt = np.linalg.svd(Fc, full_matrices=False)
    n = min(N_PC, Vt.shape[0])
    eofs = Vt[:n]
    pcs = (F - Fb.mean(axis=0, keepdims=True)) @ eofs.T
    sd = pcs[base_mask].std(axis=0)
    sd = np.where(sd < 1e-9, 1.0, sd)
    pcs = pcs / sd
    km = KMeans(n_clusters=K_REGIMES, n_init=10, random_state=SEED).fit(pcs[base_mask])
    labels = km.predict(pcs)
    cent_sort = np.argsort(km.cluster_centers_[:, 0])
    remap = {int(cent_sort[i]): i for i in range(K_REGIMES)}
    labels = np.array([remap[int(l)] for l in labels], dtype="int8")
    ds = xr_out(pcs.astype("float32"), labels, idx)
    store.write("regimes", ds)
    return {"months": int(T), "regimes": {int(k): int((labels == k).sum()) for k in range(K_REGIMES)}}


def xr_out(pcs, labels, idx):
    import xarray as xr

    data = {f"zpc{i+1}": (("time",), pcs[:, i]) for i in range(pcs.shape[1])}
    data["regime"] = (("time",), labels)
    return xr.Dataset(data, coords={"time": idx.to_timestamp()})
