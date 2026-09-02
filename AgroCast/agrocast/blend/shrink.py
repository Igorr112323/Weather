import numpy as np
import pandas as pd

from agrocast.backtest.metrics import rps_rows

NEUTRAL = 0.30
W_MAX = 0.25
WIN = 8
MIN_YEARS = 4
MIN_HIST = 10
SEASON_OF = {12: "DJF", 1: "DJF", 2: "DJF", 3: "MAM", 4: "MAM", 5: "MAM",
             6: "JJA", 7: "JJA", 8: "JJA", 9: "SON", 10: "SON", 11: "SON"}


def shrink_weight(s):
    if s is None or not np.isfinite(s):
        return 0.0
    if s >= NEUTRAL:
        return 0.0
    return float(min(W_MAX, W_MAX * (NEUTRAL - s) / NEUTRAL))


def mix_shrink(P, clim, w):
    P = np.asarray(P, float)
    clim = np.asarray(clim, float)
    if w <= 0:
        return P
    out = (1.0 - w) * P + w * clim
    if out.ndim == 1:
        return out / out.sum()
    return out / out.sum(axis=1, keepdims=True)


def _tercile(z, e1, e2):
    return int(0 if z < e1 else (1 if z <= e2 else 2))


def clim_dist(std, v, mode, month, year):
    if mode == "seasonal":
        rows = []
        for p, r in std.iterrows():
            if int(p.year) >= int(year) or int(p.month) != int(month):
                continue
            rows.append(_tercile(float(r["z"]), float(r["e1"]), float(r["e2"])))
    else:
        rows = []
        for p, r in std.iterrows():
            if int(p.year) >= int(year) or int(p.month) != int(month):
                continue
            rows.append(_tercile(float(r["z"]), float(r["e1"]), float(r["e2"])))
    if len(rows) < MIN_HIST:
        return np.array([1.0 / 3.0, 1.0 / 3.0, 1.0 / 3.0])
    c = np.array([(np.asarray(rows) == t).sum() for t in range(3)], float)
    return (c + 0.5) / (c.sum() + 1.5)


def year_skills(P, obs, yrows):
    P = np.asarray(P, float)
    obs = np.asarray(obs, int)
    yrows = np.asarray(yrows, int)
    rps_p = rps_rows(P, obs)
    rps_c = rps_rows(np.tile([1.0 / 3.0] * 3, (len(obs), 1)), obs)
    years = sorted(set(yrows.tolist()))
    out = {}
    for y in years:
        m = yrows == y
        out[y] = 1.0 - float(rps_p[m].mean()) / float(rps_c[m].mean())
    return out


def window_skill(ys, year):
    lo = int(year) - WIN
    vals = [ys[y] for y in ys if lo <= y < int(year)]
    if len(vals) < MIN_YEARS:
        return None
    return float(np.mean(vals))


class ShrinkContext:
    def __init__(self, mode, pt, v, skill_map):
        self.mode = mode
        self.pt = pt
        self.v = v
        self.skill_map = skill_map
        self._std = None

    def _get_std(self):
        if self._std is None:
            self._std = self.pt.seasonal_std(self.v, 3) if self.mode == "seasonal" else self.pt.standardized(self.v)
        return self._std

    def apply(self, P, month, year, lead=None):
        s = window_skill(self.skill_map, year)
        if s is None:
            return P
        clim = clim_dist(self._get_std(), self.v, self.mode, month, year)
        return mix_shrink(P, clim, shrink_weight(s))


def shrink_ledger_block(P, obs, yrows, pt, v, mode, months):
    ys = year_skills(P, obs, yrows)
    ctx = ShrinkContext(mode, pt, v, ys)
    out = np.empty_like(P, float)
    for i, m in enumerate(months):
        out[i] = ctx.apply(P[i], m, int(yrows[i]))
    return out
