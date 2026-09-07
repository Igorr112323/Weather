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


ECE_BINS = 10
CONFIDENCE_LOW = 0.02
CONFIDENCE_MEDIUM = 0.06
PROMOTION_RULE = "RPSS > 0 и нижняя граница 95% block-bootstrap CI > 0 (METRICS.md, согласовано 2026-09-06)"
BASELINE_DEFINITION = "empirical tercile climatology per target month over the verified span (prior +1 smoothing)"


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


def tercile_baseline(obs, months=None, prior=1.0):
    obs = np.asarray(obs, int)
    months = None if months is None else np.asarray(months, int)
    out = np.empty((len(obs), 3), float)
    if months is None:
        months = np.zeros(len(obs), int)
    for m in np.unique(months):
        sel = months == m
        vals = obs[sel]
        counts = np.array([(vals == k).sum() for k in range(3)], float)
        p = (counts + float(prior)) / (counts.sum() + 3.0 * float(prior))
        out[sel] = p
    return out


def clim_rps(obs):
    obs = np.asarray(obs, int)
    p = np.full((len(obs), 3), 1.0 / 3.0)
    return rps_mean(p, obs)


def rpss(probs, obs, months=None, baseline="empirical", weights=None):
    probs = np.asarray(probs, float)
    obs = np.asarray(obs, int)
    if baseline == "empirical":
        base = rps_rows(tercile_baseline(obs, months), obs)
    elif baseline == "uniform":
        base = np.full(len(obs), clim_rps(obs))
    else:
        raise ValueError(f"unknown baseline {baseline!r}")
    r = rps_rows(probs, obs)
    w = None if weights is None else np.asarray(weights, float)
    if w is None:
        num, den = float(r.mean()), float(base.mean())
    else:
        s = float(w.sum())
        num = float((r * w).sum() / s) if s > 0 else float(r.mean())
        den = float((base * w).sum() / s) if s > 0 else float(base.mean())
    return 1.0 - num / den if den > 1e-12 else 0.0


def weighted_rpss(probs, obs, w, months=None, baseline="empirical"):
    return rpss(probs, obs, months=months, baseline=baseline, weights=w)


def ece(probs, obs, n_bins=ECE_BINS):
    probs = np.asarray(probs, float)
    obs = np.asarray(obs, int)
    n_bins = int(n_bins)
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    idx = np.clip((probs.max(axis=1) * n_bins).astype(int), 0, n_bins - 1)
    dom = probs.argmax(axis=1)
    hit = (dom == obs).astype(float)
    top_num, top_den = 0.0, 0
    cls_num, cls_den = 0.0, 0
    counts = []
    for b in range(n_bins):
        msk = idx == b
        n = int(msk.sum())
        counts.append(n)
        if n < 5:
            continue
        top_num += n * abs(float(probs.max(axis=1)[msk].mean()) - float(hit[msk].mean()))
        top_den += n
        for k in range(3):
            cls_num += n * abs(float(probs[msk, k].mean()) - float((obs[msk] == k).mean()))
            cls_den += n
    return {
        "top_label": round(top_num / top_den, 6) if top_den else None,
        "classwise": round(cls_num / cls_den, 6) if cls_den else None,
        "n_bins": n_bins,
        "edges": [float(x) for x in edges],
        "bin_counts": counts,
        "n_used_top": int(top_den),
        "n_used_classwise": int(cls_den),
        "n_rows": int(len(obs)),
        "definition": "top-label ECE over the dominant class prob with uniform [0,1] bins (skip bins with <5 rows); classwise ECE averaged over classes and bins",
    }


def wilson_interval(k, n, z=1.96):
    k, n = float(k), float(n)
    if n <= 0:
        return [None, None]
    p = k / n
    d = 1.0 + z * z / n
    c = p + z * z / (2.0 * n)
    hw = z * float(np.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n)))
    return [float(max(0.0, (c - hw) / d)), float(min(1.0, (c + hw) / d))]


def block_means(values, block_len):
    arr = np.asarray(values, float)
    n = len(arr)
    if n == 0:
        return np.zeros(0)
    bl = max(1, int(block_len))
    if bl >= n:
        return np.array([float(arr.mean())])
    out = np.empty(n - bl + 1)
    cs = np.concatenate([[0.0], np.cumsum(arr)])
    for i in range(n - bl + 1):
        out[i] = float((cs[i + bl] - cs[i]) / bl)
    return out


def block_bootstrap_ci(values, block_len, n_boot=400, seed=7):
    values = np.asarray(values, float)
    blocks = block_means(values, block_len)
    if len(values) == 0 or len(blocks) == 0:
        return {"ci95": [None, None], "n_blocks": 0, "block_len": int(block_len)}
    point = float(values.mean())
    if len(blocks) == 1:
        return {"ci95": [point, point], "n_blocks": 1, "block_len": int(block_len)}
    k = max(1, int(np.ceil(len(values) / max(1, int(block_len)))))
    rng = np.random.default_rng(int(seed))
    boot = np.empty(int(n_boot))
    for b in range(int(n_boot)):
        boot[b] = float(blocks[rng.integers(0, len(blocks), size=k)].mean())
    lo, hi = np.quantile(boot, [0.025, 0.975])
    return {"ci95": [float(lo), float(hi)], "n_blocks": int(k), "block_len": int(block_len)}


def promotion_decision(rpss_value, ci95):
    promoted = bool(
        rpss_value is not None
        and np.isfinite(rpss_value)
        and rpss_value > 0.0
        and ci95 is not None
        and ci95[0] is not None
        and float(ci95[0]) > 0.0
    )
    return {"promoted": promoted, "rule": PROMOTION_RULE}


def year_weights(years, half_life_years):
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
