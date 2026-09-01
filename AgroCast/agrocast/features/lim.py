import numpy as np
import pandas as pd

LAMBDA_FRAC = 0.05
FALLBACK_GAMMA = 0.97


def fit_lim_monthly(pcs, until):
    df = pcs[pcs.index < until].dropna()
    cols = list(pcs.columns)
    k = len(cols)
    if len(df) < 40 or k == 0:
        return None, cols
    X = df.to_numpy(float)
    idx = df.index
    nxt = {t: t + 1 for t in idx if (t + 1) in df.index}
    A = {}
    for m in range(1, 13):
        pairs = [(t, nxt[t]) for t in nxt if t.month == m]
        if len(pairs) >= 8:
            xt = df.loc[[p[0] for p in pairs]].to_numpy(float)
            x1 = df.loc[[p[1] for p in pairs]].to_numpy(float)
            C0 = xt.T @ xt
            M1 = x1.T @ xt
            lam = LAMBDA_FRAC * np.trace(C0) / k
            A[m] = M1 @ np.linalg.inv(C0 + lam * np.eye(k))
        else:
            A[m] = None
    Xg0 = X[:-1].T @ X[:-1]
    Xg1 = X[1:].T @ X[:-1]
    lam_g = LAMBDA_FRAC * np.trace(Xg0) / k
    Ag = Xg1 @ np.linalg.inv(Xg0 + lam_g * np.eye(k))
    for m in range(1, 13):
        if A[m] is None:
            A[m] = Ag
    return A, cols


def lim_chain(A, x0, start_month, horizon):
    x = np.asarray(x0, float).copy()
    m = start_month
    for _ in range(horizon):
        x = A[m] @ x
        m = m % 12 + 1
    return x


def lim_forecast_frame(pcs, index, horizons=(1, 2, 3, 4, 5, 6)):
    cols = list(pcs.columns)
    out_cols = [f"{c}_f{h}" for h in horizons for c in cols]
    out = pd.DataFrame(np.nan, index=index, columns=out_cols, dtype=float)
    src = pcs.dropna()
    if len(src) < 40:
        return out
    arr = {t: src.loc[t].to_numpy(float) for t in src.index}
    issues = [t for t in index if t in arr]
    cache = {}
    for t in issues:
        y = t.year
        if y not in cache:
            A, _ = fit_lim_monthly(src, pd.Period(f"{y}-01", "M"))
            if A is None:
                continue
            cache[y] = A
    for t in issues:
        A = cache.get(t.year)
        if A is None:
            continue
        x0 = arr[t]
        for h in horizons:
            xf = lim_chain(A, x0, t.month, h)
            for j, c in enumerate(cols):
                out.loc[t, f"{c}_f{h}"] = xf[j]
    return out


def persistence_frame(pcs, index, horizons=(1, 2, 3, 4, 5, 6)):
    cols = list(pcs.columns)
    out = pd.DataFrame(np.nan, index=index, columns=[f"{c}_f{h}" for h in horizons for c in cols], dtype=float)
    src = pcs.dropna()
    for t in index:
        if t not in src.index:
            continue
        for h in horizons:
            tgt = t + h
            if tgt not in src.index:
                continue
            for c in cols:
                out.loc[t, f"{c}_f{h}"] = src.loc[tgt, c]
    return out
