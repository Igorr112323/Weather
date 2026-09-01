import numpy as np


def snap(lat, lon, lats, lons):
    lats = np.asarray(lats, float)
    lons = np.asarray(lons, float)
    ilat = int(np.argmin(np.abs(lats - lat)))
    ilon = int(np.argmin(np.abs(((lons - lon + 180) % 360) - 180)))
    return float(lats[ilat]), float(lons[ilon])


def haversine(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dp = p2 - p1
    dl = np.radians(lon2 - lon1)
    a = np.sin(dp / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return 2 * r * np.arcsin(np.sqrt(a))


def cos_lat(lats):
    return np.sqrt(np.clip(np.cos(np.radians(np.asarray(lats, float))), 0.0, 1.0))


def bbox_around(lat, lon, deg=1.5):
    return lat - deg, lat + deg, lon - deg, lon + deg


def region_center(region):
    return 0.5 * (region.lat_min + region.lat_max), 0.5 * (region.lon_min + region.lon_max)
