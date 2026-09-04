import numpy as np

from agrocast.region.kriging import (
    Variogram,
    exponential_variogram,
    empirical_variogram,
    fit_variogram,
    ordinary_kriging,
    idw,
    pairwise_km,
)


def test_variogram_shapes():
    vg = Variogram(nugget=0.2, psill=0.8, range_km=100.0)
    assert exponential_variogram(0.0, 0.2, 0.8, 100.0) == 0.0
    g = np.array([vg.gamma(h) for h in (1.0, 50.0, 200.0, 2000.0)])
    assert np.all(np.diff(g) > 0)
    assert abs(g[-1] - vg.sill) < 1e-8
    assert abs(vg.cov(0.0) - vg.sill) < 1e-12
    assert abs(vg.cov(2000.0)) < 1e-8
    rng = np.random.default_rng(11)
    h = np.linspace(2.0, 300.0, 14)
    y = exponential_variogram(h, 0.15, 0.85, 90.0) + rng.normal(0, 0.01, len(h))
    from scipy.optimize import curve_fit

    popt, _ = curve_fit(lambda x, c0, s, r: exponential_variogram(x, c0, s, r),
                        h, y, p0=[0.0, 1.0, 50.0], bounds=([0, 1e-9, 1], [np.inf] * 3))
    assert abs(popt[0] - 0.15) < 0.03 and abs(popt[2] - 90.0) < 15.0


def test_fit_variogram_on_smooth_field():
    rng = np.random.default_rng(5)
    pts = np.column_stack([rng.uniform(44.5, 46.0, 24), rng.uniform(37.5, 40.0, 24)])
    base = np.sin(pairwise_km(pts).mean(axis=0) / 150.0)
    vals = base + rng.normal(0, 0.05, len(pts))
    vg = fit_variogram(pts, vals)
    assert vg.psill > 0.0 and vg.range_km > 1.0
    assert vg.nugget >= 0.0
    d, g, n = empirical_variogram(pts, vals)
    assert (n > 0).all() and len(d) >= 3


def test_exact_interpolation_at_data_points():
    rng = np.random.default_rng(3)
    pts = np.column_stack([rng.uniform(44.25, 46.25, 10), rng.uniform(37.25, 40.25, 10)])
    vals = rng.normal(0, 1, len(pts))
    vg = Variogram(nugget=0.0, psill=1.0, range_km=150.0)
    res = ordinary_kriging(pts, vals, pts, variogram=vg, detrend=False)
    assert (res.method == "ok").all()
    assert np.max(np.abs(res.pred - vals)) < 1e-6
    assert np.allclose(res.var, 0.0, atol=1e-6)
    if res.weights.size:
        assert np.allclose(res.weights[:, :-1].sum(axis=1), 1.0, atol=1e-8)


def test_constant_field_zero_variance():
    pts = np.array([[45.0, 38.0], [45.5, 38.5], [46.0, 39.0], [45.25, 39.75]])
    res = ordinary_kriging(pts, np.full(4, 7.5), np.array([[45.4, 38.8], [45.9, 40.0]]), detrend=False)
    assert np.allclose(res.pred, 7.5, atol=1e-8)
    assert np.all(np.isfinite(res.var)) and np.all(res.var >= 0.0)


def test_plane_detrend_recovers_trend():
    lat = np.repeat(np.arange(44.25, 46.5, 0.5), 5)
    lon = np.tile(np.arange(37.25, 40.5, 0.75), 5)
    pts = np.column_stack([lat, lon])
    plane = 3.0 * lat - 1.5 * lon + 10.0
    targets = np.array([[45.4, 38.9], [44.6, 39.9], [46.1, 37.6]])
    exact = 3.0 * targets[:, 0] - 1.5 * targets[:, 1] + 10.0
    vg = Variogram(nugget=0.0, psill=0.01, range_km=50.0)
    on = ordinary_kriging(pts, plane, targets, variogram=vg, detrend=True)
    off = ordinary_kriging(pts, plane, targets, variogram=vg, detrend=False)
    assert np.max(np.abs(on.pred - exact)) < 1e-6
    assert np.max(np.abs(on.pred - exact)) < np.max(np.abs(off.pred - exact))


def test_idw_fallback_on_duplicates():
    pts = np.array([[45.0, 38.0], [45.0, 38.0], [45.5, 38.6], [46.0, 39.2]])
    vals = np.array([1.0, 3.0, 2.0, 0.5])
    res = ordinary_kriging(pts, vals, np.array([[45.0, 38.0], [45.6, 38.8]]), detrend=False)
    assert (res.method == "idw").all()
    assert res.n_fallback == 2
    assert abs(res.pred[0] - vals[0]) < 1e-9
    res2 = ordinary_kriging(pts[:2], vals[:2], np.array([[45.2, 38.2]]), detrend=False)
    assert (res2.method == "idw").all()
    p, _ = idw(pts, vals, np.array([[45.0, 38.0]]))
    assert abs(p[0] - 1.0) < 1e-9
