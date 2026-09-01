import io
import numpy as np
import pandas as pd
import requests
import xarray as xr

from agrocast.core.config import Config
from agrocast.ingest.registry import Registry
from agrocast.store.zarrstore import ZarrStore

NCEI_URL = "https://www.ncei.noaa.gov/access/services/data/v1"


def fetch_station(station_id, start_date, end_date, data_types=("PRCP", "TAVG", "TMAX", "TMIN")):
    params = {
        "dataset": "daily-summaries",
        "stations": station_id,
        "startDate": str(start_date),
        "endDate": str(end_date),
        "dataTypes": ",".join(data_types),
        "units": "metric",
        "format": "csv",
        "includeAttributes": "false",
    }
    r = requests.get(NCEI_URL, params=params, timeout=120)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    df["DATE"] = pd.to_datetime(df["DATE"])
    return df.set_index("DATE")


def to_daily_frame(df):
    out = pd.DataFrame(index=df.index)
    if {"TMAX", "TMIN"}.issubset(df.columns):
        out["t2m"] = (df["TMAX"] + df["TMIN"]) / 2.0
    elif "TAVG" in df.columns:
        out["t2m"] = df["TAVG"]
    if "PRCP" in df.columns:
        out["tp"] = df["PRCP"]
    return out.dropna(how="all")


def monthly_station(daily):
    p = pd.PeriodIndex(daily.index, freq="M")
    out = {}
    if "t2m" in daily:
        out["t2m"] = daily["t2m"].groupby(p).mean()
    if "tp" in daily:
        out["tp"] = daily["tp"].groupby(p).sum()
    return pd.DataFrame(out)


def save_station(config, station_id, daily):
    store = ZarrStore(config.zarr_dir)
    reg = Registry(config.registry_path)
    df = daily.dropna(how="all").sort_index()
    name = f"station_{station_id}"
    idx = pd.DatetimeIndex(df.index.values)
    ds = xr.Dataset(
        {c: (("time",), df[c].astype("float32").to_numpy()) for c in df.columns},
        coords={"time": idx},
    )
    store.write(name, ds)
    reg.upsert_file("ncei", name, str(store.last_time(name)))
    return name


def update_station(config, station_id, start_date=None, end_date=None):
    end = end_date or pd.Timestamp.now().strftime("%Y-%m-%d")
    start = start_date or "1991-01-01"
    raw = fetch_station(station_id, start, end)
    daily = to_daily_frame(raw)
    name = save_station(config, station_id, daily)
    return {"name": name, "rows": len(daily)}


def load_station(config, station_id):
    store = ZarrStore(config.zarr_dir)
    name = f"station_{station_id}"
    if not store.exists(name):
        return None
    ds = store.open(name)
    ds = ds.drop_vars([v for v in ds.coords if v != "time"], errors="ignore")
    df = ds.to_dataframe()
    return df[[c for c in ["t2m", "tp"] if c in df.columns]]


def calibrate_point(monthly_grid, monthly_st):
    joined = monthly_grid.join(monthly_st, lsuffix="_grid", rsuffix="_st", how="inner").dropna()
    out = {}
    for v in ["t2m", "tp"]:
        g, s = f"{v}_grid", f"{v}_st"
        if g not in joined or s not in joined or len(joined) < 60:
            continue
        if v == "tp":
            ratio = float(joined[s].sum() / max(joined[g].sum(), 1e-9))
            out[v] = {"kind": "ratio", "value": ratio}
        else:
            out[v] = {"kind": "bias", "value": float((joined[s] - joined[g]).mean())}
    return out


STATION_CATALOG = [
    {"id": "RSM00037031", "name": "Krasnodar", "lat": 45.03, "lon": 39.15},
    {"id": "RSM00037099", "name": "Yeysk", "lat": 46.71, "lon": 38.27},
    {"id": "RSM00037021", "name": "Armavir", "lat": 44.99, "lon": 41.12},
    {"id": "RSM00037171", "name": "Sochi", "lat": 43.43, "lon": 39.92},
    {"id": "RSM00034730", "name": "Rostov-na-Donu", "lat": 47.26, "lon": 39.70},
]


def nearest_station(lat, lon):
    from agrocast.core.geo import haversine

    best, bd = None, 1e9
    for s in STATION_CATALOG:
        d = float(haversine(lat, lon, s["lat"], s["lon"]))
        if d < bd:
            best, bd = s, d
    return best, bd


def station_monthly_cached(config, station_id, start="1991-01-01"):
    df = load_station(config, station_id)
    if df is None or len(df) < 200:
        try:
            update_station(config, station_id, start_date=start)
            df = load_station(config, station_id)
        except Exception:
            return None
    if df is None or len(df) < 200:
        return None
    p = pd.PeriodIndex(df.index, freq="M")
    out = {}
    g = df.groupby(p)
    if "t2m" in df.columns:
        out["t2m"] = g["t2m"].mean()
    if "tp" in df.columns:
        out["tp"] = g["tp"].sum()
    return pd.DataFrame(out).sort_index()


def validation_report(grid_monthly, station_monthly):
    joined = grid_monthly.join(station_monthly, lsuffix="_g", rsuffix="_s", how="inner").dropna()
    rep = {"n_months": int(len(joined))}
    for v in ["t2m", "tp"]:
        g, s = f"{v}_g", f"{v}_s"
        if g not in joined or s not in joined or len(joined) < 40:
            continue
        corr = float(np.corrcoef(joined[g], joined[s])[0, 1])
        if v == "tp":
            rep["tp"] = {
                "corr": round(corr, 3),
                "grid_mean_mm": round(float(joined[g].mean()), 1),
                "station_mean_mm": round(float(joined[s].mean()), 1),
                "ratio_station_grid": round(float(joined[s].sum() / max(joined[g].sum(), 1e-9)), 3),
            }
        else:
            rep["t2m"] = {
                "corr": round(corr, 3),
                "bias_station_grid_c": round(float((joined[s] - joined[g]).mean()), 2),
            }
    return rep


def calibration_for_point(config, lat, lon, grid_monthly):
    st, dist = nearest_station(lat, lon)
    if dist > 250.0:
        return None
    sm = station_monthly_cached(config, st["id"])
    if sm is None:
        return None
    rep = validation_report(grid_monthly, sm)
    out = {
        "station": st["name"],
        "station_id": st["id"],
        "distance_km": round(dist, 1),
        "n_months": rep.get("n_months", 0),
        "t2m_bias": rep.get("t2m", {}).get("bias_station_grid_c"),
        "tp_ratio": rep.get("tp", {}).get("ratio_station_grid"),
        "validation": rep,
    }
    if out["t2m_bias"] is None and out["tp_ratio"] is None:
        return None
    return out
