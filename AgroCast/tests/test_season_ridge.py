import numpy as np
import pandas as pd

from agrocast.models.builder import build_models
from agrocast.models.season_ridge import SeasonRidge


def make_X(n=60, seed=3, tmonth=12.0):
    rng = np.random.default_rng(seed)
    a = rng.normal(0, 1, n)
    b = rng.normal(0, 1, n)
    y = 0.9 * a + rng.normal(0, 0.3, n)
    X = pd.DataFrame({"feat_a": a, "feat_b": b, "lead": [0.25] * n, "tmonth": [tmonth] * n})
    return X, y


def test_inactive_returns_clim():
    X, y = make_X(tmonth=6.0)
    m = SeasonRidge("phys_mam", (3, 4, 5), ["feat_a", "feat_b"])
    m.fit(X, y)
    p, q = m.predict(X.iloc[[0]], 0.4, 0.4)
    assert m._ridge is None
    assert len(p) == 3 and abs(p.sum() - 1.0) < 1e-6
    assert len(q) == 3


def test_active_uses_feature():
    X, y = make_X(tmonth=12.0)
    m = SeasonRidge("phys_djf", (12, 1, 2), ["feat_a", "feat_b"])
    m.fit(X, y)
    assert m._ridge is not None
    Xp = X.copy()
    Xp.loc[0, "feat_a"] = 2.0
    Xp.loc[1, "feat_a"] = -2.0
    p_hi, _ = m.predict(Xp.iloc[[0]], 0.4, 0.4)
    p_lo, _ = m.predict(Xp.iloc[[1]], 0.4, 0.4)
    assert p_hi[2] > p_lo[2] + 0.15
    assert abs(p_hi.sum() - 1.0) < 1e-6


def test_missing_features_falls_back():
    X, y = make_X(tmonth=12.0)
    m = SeasonRidge("phys_djf", 12, ["nope_1", "nope_2"])
    m.fit(X, y)
    assert m._ridge is None
    p, _ = m.predict(X.iloc[[0]], 0.4, 0.4)
    assert len(p) == 3 and abs(p.sum() - 1.0) < 1e-6


def test_nan_feature_falls_back():
    X, y = make_X(tmonth=12.0)
    m = SeasonRidge("phys_djf", (12, 1, 2), ["feat_a", "feat_b"])
    m.fit(X, y)
    Xn = X.copy()
    Xn.loc[0, "feat_b"] = np.nan
    p, _ = m.predict(Xn.iloc[[0]], 0.4, 0.4)
    assert len(p) == 3 and abs(p.sum() - 1.0) < 1e-6


def test_build_models_gating():
    from agrocast.core.config import Config

    cfg = Config()
    t2m = [m.name for m in build_models(cfg, variable="t2m", mode="seasonal")]
    tp_s = [m.name for m in build_models(cfg, variable="tp", mode="seasonal")]
    tp_m = [m.name for m in build_models(cfg, variable="tp", mode="monthly")]
    assert "phys_djf" not in t2m
    assert "phys_mam" not in t2m
    assert not [n for n in tp_m if n.startswith("phys_")]
    assert set(tp_s) - set(t2m) == {"phys_djf", "phys_mam"}
