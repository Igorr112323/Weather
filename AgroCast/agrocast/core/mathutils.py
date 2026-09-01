import numpy as np

try:
    from scipy.special import ndtr
except ImportError:
    from math import erf, sqrt

    def ndtr(x):
        return 0.5 * (1.0 + erf(np.asarray(x, float) / sqrt(2.0)))


def exp_weights(n, half_life):
    n = int(n)
    if n <= 0:
        return np.zeros(0)
    ages = np.arange(n)[::-1].astype(float)
    w = 0.5 ** (ages / max(float(half_life), 1e-9))
    s = w.sum()
    return w / s if s > 0 else np.full(n, 1.0 / n)


def weighted_quantile(values, qs, w=None):
    v = np.asarray(values, float)
    q = np.atleast_1d(np.asarray(qs, float))
    if len(v) == 0:
        return np.full(len(q), np.nan)
    if w is None:
        return np.quantile(v, q)
    w = np.asarray(w, float)
    s = np.argsort(v)
    v = v[s]
    w = w[s]
    cw = np.cumsum(w)
    tot = cw[-1]
    if tot <= 0:
        return np.quantile(v, q)
    cw = cw / tot
    return np.interp(q, cw, v)


def weighted_mean_std(values, w=None):
    v = np.asarray(values, float)
    if w is None:
        return float(v.mean()), float(v.std(ddof=0))
    w = np.asarray(w, float)
    tot = w.sum()
    mu = float((v * w).sum() / tot)
    var = float((w * (v - mu) ** 2).sum() / tot)
    return mu, float(np.sqrt(max(var, 0.0)))


def tercile_probs_normal(mu, sd, e1, e2):
    sd = max(float(sd), 0.05)
    p0 = float(ndtr((e1 - mu) / sd))
    p2 = float(1.0 - ndtr((e2 - mu) / sd))
    p1 = max(1.0 - p0 - p2, 0.0)
    p = np.clip(np.array([p0, p1, p2]), 0.02, None)
    return p / p.sum()


def weighted_tercile_probs(z, e1, e2, w=None):
    z = np.asarray(z, float)
    if len(z) == 0:
        return np.full(3, 1.0 / 3.0)
    w = np.ones(len(z)) if w is None else np.asarray(w, float)
    tot = w.sum()
    if tot <= 0:
        return np.full(3, 1.0 / 3.0)
    p0 = float(w[z < e1].sum() / tot)
    p2 = float(w[z > e2].sum() / tot)
    p1 = max(1.0 - p0 - p2, 0.0)
    p = np.clip(np.array([p0, p1, p2]), 0.02, None)
    return p / p.sum()


def softmax_w(x, temp=0.05):
    x = np.asarray(x, float)
    if len(x) == 0:
        return x
    t = max(float(temp), 1e-6)
    z = (x - x.max()) / t
    e = np.exp(z)
    return e / e.sum()


def clip_probs(p):
    p = np.asarray(p, float)
    p = np.clip(p, 0.01, None)
    return p / p.sum()


def monotonize_q(q):
    q = np.sort(np.asarray(q, float))
    return q
