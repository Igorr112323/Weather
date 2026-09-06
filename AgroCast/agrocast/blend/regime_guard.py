import numpy as np
import pandas as pd

WINDOW_M = 24
REF_M = 240
WINDOW_S = 8
REF_S = 80
SEASON_STARTS = (1, 4, 7, 10)


def enabled(config=None):
    return config.regime_guard if config is not None else True


def _terc(z, e1, e2):
    if z < e1:
        return 0
    if z <= e2:
        return 1
    return 2


def _month_terc(series, std, p):
    try:
        r = std.loc[p]
        x = float(series.loc[p])
    except (KeyError, TypeError):
        return None
    if not np.isfinite(x) or not np.isfinite(r["mu"]) or not np.isfinite(r["sd"]) or float(r["sd"]) <= 0:
        return None
    z = (x - float(r["mu"])) / float(r["sd"])
    return _terc(z, float(r["e1"]), float(r["e2"]))


def _season_terc(series, std, m0, v):
    months = [m0 + i for i in range(3)]
    vals = []
    for mm in months:
        try:
            x = float(series.loc[mm])
        except (KeyError, TypeError):
            return None
        if not np.isfinite(x):
            return None
        vals.append(x)
    try:
        r = std.loc[m0]
    except (KeyError, TypeError):
        return None
    if not np.isfinite(r["mu"]) or not np.isfinite(r["sd"]) or float(r["sd"]) <= 0:
        return None
    val = float(np.mean(vals)) if v == "t2m" else float(np.sum(vals))
    z = (val - float(r["mu"])) / float(r["sd"])
    return _terc(z, float(r["e1"]), float(r["e2"]))


def _smooth(counts, n):
    c = np.asarray(counts, float)
    return (c + 1.0) / (n + 3.0)


def _window_and_ref(series, std, v, mode, issue, tgt):
    if mode == "monthly":
        win = []
        for k in range(WINDOW_M):
            t = _month_terc(series, std, issue - k)
            if t is not None:
                win.append(t)
        ref = []
        for k in range(WINDOW_M, WINDOW_M + REF_M):
            t = _month_terc(series, std, issue - k)
            if t is not None:
                ref.append(t)
        return win, ref
    m0 = tgt
    win = []
    for k in range(1, WINDOW_S + 1):
        t = _season_terc(series, std, m0 - 3 * k, v)
        if t is not None:
            win.append(t)
    ref = []
    for k in range(WINDOW_S + 1, WINDOW_S + REF_S + 1):
        t = _season_terc(series, std, m0 - 3 * k, v)
        if t is not None:
            ref.append(t)
    return win, ref


def _modal_flip(series, std, v, mode, issue, tgt):
    win, ref = _window_and_ref(series, std, v, mode, issue, tgt)
    if len(win) < 12 or len(ref) < 60:
        return False
    q = _smooth(np.bincount(win, minlength=3), len(win))
    h = _smooth(np.bincount(ref, minlength=3), len(ref))
    return int(np.argmax(q)) != int(np.argmax(h))


def shifted(series, std, v, mode, issue, tgt=None, config=None):
    if not enabled(config):
        return False
    return _modal_flip(series, std, v, mode, issue, tgt)


def shifted_rows(series, std, v, mode, rows, config=None):
    n = len(rows)
    out = np.zeros(n, dtype=bool)
    if not enabled(config):
        return out
    years = rows["year"].to_numpy(int)
    tms = rows["target_month"].to_numpy(int)
    leads = rows["lead"].to_numpy(int) if "lead" in rows.columns else np.ones(n, int)
    for i in range(n):
        y, tm, ld = int(years[i]), int(tms[i]), int(leads[i])
        tgt = pd.Period(f"{y}-{tm:02d}", "M")
        issue = tgt - 1 if mode == "seasonal" else tgt - int(ld)
        out[i] = _modal_flip(series, std, v, mode, issue, tgt if mode == "seasonal" else None)
    return out
