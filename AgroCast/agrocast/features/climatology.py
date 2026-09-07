import numpy as np
import pandas as pd

from agrocast.core.mathutils import exp_weights, weighted_quantile, weighted_mean_std

QS = (0.1, 0.25, 0.33, 0.5, 0.67, 0.75, 0.9)


def monthly_from_daily(daily):
    p = pd.PeriodIndex(daily.index, freq="M")
    out = {}
    g = daily.groupby(p)
    if "t2m" in daily:
        out["t2m"] = g["t2m"].mean()
    if "tp" in daily:
        out["tp"] = g["tp"].sum(min_count=1)
    if "swvl" in daily:
        out["swvl"] = g["swvl"].mean()
    if "snow" in daily:
        out["snow"] = g["snow"].mean()
    return pd.DataFrame(out).sort_index()


def adaptive(series, year, month, window=30, half_life=20.0):
    s = series.dropna()
    hist = s[(s.index.month == month) & (s.index.year <= year - 1)].tail(window)
    if len(hist) < 5:
        hist = s[s.index.month == month].tail(max(window, 8))
    if len(hist) == 0:
        return None
    w = exp_weights(len(hist), half_life)
    mu, sd = weighted_mean_std(hist.to_numpy(), w)
    sd = max(sd, 0.05)
    q = weighted_quantile(hist.to_numpy(), QS, w)
    return {"mu": mu, "sd": sd, "n": len(hist), "q": dict(zip(QS, q))}


def standardize_monthly(series, window=30, half_life=20.0):
    s = series.dropna().sort_index()
    rows = []
    for p in s.index:
        a = adaptive(s, p.year, p.month, window, half_life)
        if a is None:
            continue
        x = float(s.loc[p])
        sd = a["sd"]
        rows.append(
            {
                "period": p,
                "z": (x - a["mu"]) / sd,
                "mu": a["mu"],
                "sd": sd,
                "e1": (a["q"][0.33] - a["mu"]) / sd,
                "e2": (a["q"][0.67] - a["mu"]) / sd,
            }
        )
    return pd.DataFrame(rows).set_index("period")


def month_z(values, index, base_start, base_end):
    s = pd.Series(np.asarray(values, float), index=index)
    out = np.full(len(s), np.nan)
    for m in range(1, 13):
        vals = s[s.index.month == m]
        b = vals[(vals.index.year >= base_start) & (vals.index.year <= base_end)]
        if len(b) < 5:
            continue
        mu = float(b.mean())
        sd = max(float(b.std(ddof=0)) if len(b) > 1 else 1.0, 1e-6)
        out[np.where(s.index.month == m)[0]] = (vals.to_numpy() - mu) / sd
    return out


def past_monthly_anom(series):
    s = series.dropna().sort_index()
    vals = s.to_numpy(float)
    months = s.index.month.to_numpy()
    years = s.index.year.to_numpy()
    out = np.full(len(s), np.nan)
    for i in range(len(s)):
        hist = vals[(months == months[i]) & (years < years[i])]
        hist = hist[np.isfinite(hist)]
        if len(hist) >= 5:
            out[i] = vals[i] - hist.mean()
    return pd.Series(out, index=s.index)


def past_standardize(df, min_hist=24):
    a = df.to_numpy(float)
    out = np.full_like(a, np.nan)
    for j in range(a.shape[1]):
        col = a[:, j]
        for i in range(min_hist, len(col)):
            hist = col[:i]
            hist = hist[np.isfinite(hist)]
            if len(hist) < min_hist:
                continue
            sd = float(hist.std())
            if sd < 1e-9:
                continue
            out[i, j] = (col[i] - float(hist.mean())) / sd
    return pd.DataFrame(out, index=df.index, columns=df.columns)


def seasonal_series(monthly, var, n_months):
    agg = "sum" if var == "tp" else "mean"
    s = monthly[var].dropna()
    rows = {}
    for p in s.index:
        span = pd.period_range(p, periods=n_months, freq="M")
        if span[-1] not in s.index:
            continue
        vals = s.reindex(span).to_numpy(float)
        if np.isnan(vals).any():
            continue
        rows[p] = float(vals.sum() if agg == "sum" else vals.mean())
    return pd.Series(rows).sort_index()
