import numpy as np
import pandas as pd

try:
    from scipy.stats import gamma as _gamma
    from scipy.special import ndtri
except ImportError:
    _gamma = None
    ndtri = None

DOY_KEYS = {"spring_frost_doy", "autumn_frost_doy"}


def _max_run(mask):
    best = 0
    cur = 0
    for v in mask:
        cur = cur + 1 if v else 0
        best = max(best, cur)
    return best


def daily_indices(df):
    pr = df["tp"].to_numpy(float)
    t = df["t2m"].to_numpy(float)
    tmin = df["tmin"].to_numpy(float) if "tmin" in df else t - 4.0
    tmax = df["tmax"].to_numpy(float) if "tmax" in df else t + 4.0
    warm = t > 10.0
    denom = 0.1 * t[warm].sum()
    htk = float(pr[warm].sum() / denom) if denom > 1e-9 else np.nan
    cold = np.where(tmin < 0.0)[0]
    dates = df.index
    spring = [dates[i] for i in cold if dates[i].month <= 6]
    autumn = [dates[i] for i in cold if dates[i].month >= 7]
    return {
        "total_precip": float(pr.sum()),
        "htk": htk,
        "gdd5": float(np.clip(t - 5.0, 0, None).sum()),
        "gdd10": float(np.clip(t - 10.0, 0, None).sum()),
        "dry_spell_max": float(_max_run(pr < 1.0)),
        "days_pr_gt_10": int((pr > 10.0).sum()),
        "days_pr_gt_20": int((pr > 20.0).sum()),
        "hot_days_30": int((tmax > 30.0).sum()),
        "hot_days_35": int((tmax > 35.0).sum()),
        "spring_frost_doy": float(spring[-1].dayofyear) if spring else np.nan,
        "autumn_frost_doy": float(autumn[0].dayofyear) if autumn else np.nan,
    }


def ensemble_indices(list_of_dicts):
    keys = list(list_of_dicts[0].keys())
    out = {}
    for k in keys:
        vals = np.array([d[k] for d in list_of_dicts], float)
        vals = vals[np.isfinite(vals)]
        if len(vals) == 0:
            out[k] = None
            continue
        if k in DOY_KEYS:
            out[k] = {
                "p10": _doy_str(np.quantile(vals, 0.1)),
                "p50": _doy_str(np.quantile(vals, 0.5)),
                "p90": _doy_str(np.quantile(vals, 0.9)),
            }
        else:
            out[k] = {
                "p10": float(np.quantile(vals, 0.1)),
                "p50": float(np.quantile(vals, 0.5)),
                "p90": float(np.quantile(vals, 0.9)),
            }
    return out


def _doy_str(doy):
    d = pd.Timestamp("2001-01-01") + pd.Timedelta(days=float(doy) - 1)
    return d.strftime("%m-%d")


def spi_of_value(hist_values, x):
    if _gamma is None or ndtri is None:
        return None
    h = np.asarray(hist_values, float)
    h = h[np.isfinite(h)]
    if len(h) < 20:
        return None
    p_zero = float((h < 0.1).mean())
    wet = h[h >= 0.1]
    if len(wet) < 10:
        return None
    try:
        a, loc, scl = _gamma.fit(wet, floc=0.0)
        cdf = p_zero + (1.0 - p_zero) * _gamma.cdf(x, a, loc=0.0, scale=scl)
        return float(ndtri(np.clip(cdf, 1e-4, 1 - 1e-4)))
    except Exception:
        return None


def spi_block(monthly_tp, targets):
    out = {}
    for (year, month), qs in targets.items():
        hist = monthly_tp[monthly_tp.index.month == month].to_numpy(float)
        vals = {}
        for name, x in qs.items():
            vals[name] = spi_of_value(hist, x)
        out[f"{year}-{month:02d}"] = vals
    return out
