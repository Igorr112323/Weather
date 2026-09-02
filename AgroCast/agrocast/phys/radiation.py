import numpy as np

S0 = 1361.0
SIGMA = 5.67e-8
EPS_SURF = 0.97
CLOUD_FRAC_CLEAR = 0.62


def solar_declination(n):
    return 23.44 * np.sin(np.radians(360.0 * (284.0 + n) / 365.0))


def sw_clearsky(lat, n):
    delta = np.radians(solar_declination(n))
    phi = np.radians(lat)
    x = -np.tan(phi) * np.tan(delta)
    h0 = np.arccos(np.clip(x, -1.0, 1.0))
    toa = (S0 / np.pi) * (h0 * np.sin(phi) * np.sin(delta) + np.cos(phi) * np.cos(delta) * np.sin(h0))
    return 0.75 * toa


def lw_net_clear(t_k):
    return (CLOUD_FRAC_CLEAR - EPS_SURF) * SIGMA * t_k**4


def cloud_frac(n):
    return 0.45 + 0.20 * np.cos(2.0 * np.pi * (float(n) - 15.0) / 365.0)


def net_radiation(lat, n, t_k, albedo):
    c = cloud_frac(n)
    sw = sw_clearsky(lat, n) * (1.0 - 0.6 * c)
    lw = (0.62 + 0.25 * c - EPS_SURF) * SIGMA * np.asarray(t_k, dtype=float) ** 4
    return sw * (1.0 - albedo) + lw


def snow_albedo(age_days, fresh):
    if fresh > 0.1:
        return 0.85
    return max(0.40, 0.85 - 0.012 * age_days)
