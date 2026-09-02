import numpy as np

try:
    from scipy.stats import norm
except ImportError:
    from agrocast.core.mathutils import ndtr as _ndtr

    class norm:
        @staticmethod
        def cdf(x):
            return _ndtr(x)

        @staticmethod
        def ppf(x):
            raise NotImplementedError


def rps_rows(probs, obs):
    probs = np.asarray(probs, float)
    obs = np.asarray(obs, int)
    c = np.cumsum(probs, axis=1)
    o = np.zeros_like(c)
    o[np.arange(len(obs)), obs] = 1.0
    o = np.cumsum(o, axis=1)
    return ((c - o) ** 2).sum(axis=1)


def rps_mean(probs, obs):
    return float(rps_rows(probs, obs).mean())


def clim_rps(obs):
    obs = np.asarray(obs, int)
    p = np.full((len(obs), 3), 1.0 / 3.0)
    return rps_mean(p, obs)


def rpss(probs, obs):
    base = clim_rps(obs)
    return 1.0 - rps_mean(probs, obs) / base if base > 1e-9 else 0.0


def year_weights(years, half_life_years):
    """Экспоненциальные веса «свежести» наблюдений (полупериод в годах)."""
    years = np.asarray(years, float)
    hl = float(half_life_years)
    if hl <= 0 or len(years) == 0:
        return np.ones(len(years))
    y_max = years.max()
    w = 0.5 ** ((y_max - years) / hl)
    s = w.sum()
    return w / s if s > 0 else np.ones(len(years)) / len(years)


def weighted_rps_mean(probs, obs, w):
    r = rps_rows(probs, obs)
    w = np.asarray(w, float)
    s = w.sum()
    if s <= 0:
        return float(r.mean())
    return float((r * w).sum() / s)


def weighted_rpss(probs, obs, w):
    base = clim_rps(obs)
    val = weighted_rps_mean(probs, obs, w)
    return 1.0 - val / base if base > 1e-9 else 0.0


def brier(probs, obs, k):
    obs = np.asarray(obs, int)
    p = np.asarray(probs, float)[:, k]
    return float(((p - (obs == k)) ** 2).mean())


def crps_normal(mu, sd, obs):
    mu = np.asarray(mu, float)
    sd = np.asarray(sd, float)
    obs = np.asarray(obs, float)
    z = (obs - mu) / sd
    return float((sd * (z * (2 * norm.cdf(z) - 1) + 2 * norm.pdf(z) - 1.0 / np.sqrt(np.pi))).mean())


def corr(a, b):
    a = np.asarray(a, float)
    b = np.asarray(b, float)
    if len(a) < 3 or a.std() < 1e-9 or b.std() < 1e-9:
        return float("nan")
    return float(np.corrcoef(a, b)[0, 1])


def rmse(a, b):
    a = np.asarray(a, float)
    b = np.asarray(b, float)
    return float(np.sqrt(((a - b) ** 2).mean()))
