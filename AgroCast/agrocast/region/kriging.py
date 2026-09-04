# -*- coding: utf-8 -*-
"""Обыкновенный кринг (ordinary kriging) для полей терцилей по сетке КРА.

Модель:
- экспоненциальная вариограмма γ(h) = nugget + psill·(1 − exp(−h/range)),
  γ(0) = 0; параметры подбираются по эмпирической вариограмме данных
  (наименьшие квадраты, scipy.curve_fit);
- расстояния — гаверсинусовы, км (agrocast.core.geo.haversine);
- детрендинг плоскостью: перед крингом снимается линейный тренд
  z ~ a + b·lat + c·lon (крингятся остатки, тренд возвращается в прогнозе);
- обыкновенный кринг: система с множителем Лагранжа, Σw = 1;
- IDW-фолбэк: если точек < 3 или ковариационная матрица вырождена
  (дубли точек и т.п.) — обратные взвешенные расстояния.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from agrocast.core.geo import haversine

MIN_POINTS = 3          # меньше точек — сразу IDW
COND_LIMIT = 1e10       # предел числа обусловленности ковариационной матрицы
IDW_POWER = 2.0


@dataclass
class Variogram:
    """Параметры экспоненциальной вариограммы (расстояния в км)."""

    nugget: float
    psill: float   # частичный нагор (total sill = nugget + psill)
    range_km: float

    @property
    def sill(self):
        return float(self.nugget) + float(self.psill)

    def gamma(self, h):
        return exponential_variogram(h, self.nugget, self.psill, self.range_km)

    def cov(self, h):
        """Ковариация C(h) = sill − γ(h); C(0) = sill (эффект самородка)."""
        out = np.asarray(self.sill - self.gamma(h), dtype=float)
        return float(out) if out.ndim == 0 else out


def exponential_variogram(h, nugget, psill, range_km):
    """γ(h) = nugget + psill·(1 − exp(−h/range)); γ(0) = 0. Векторизовано."""
    h = np.asarray(h, float)
    out = float(nugget) + float(psill) * (1.0 - np.exp(-h / float(range_km)))
    out = np.where(h <= 0, 0.0, out)
    return float(out) if out.ndim == 0 else out


def pairwise_km(pts):
    """Матрица попарных расстояний (км) для массива точек (N, 2) lat/lon."""
    pts = np.asarray(pts, float)
    lat = pts[:, 0][:, None]
    lon = pts[:, 1][:, None]
    return haversine(lat, lon, lat.T, lon.T)


def cross_km(pts, targets):
    """Матрица расстояний (км) между точками данных (N,) и целями (M,)."""
    pts = np.asarray(pts, float)
    targets = np.atleast_2d(np.asarray(targets, float))
    return haversine(pts[:, 0][:, None], pts[:, 1][:, None],
                     targets[:, 0][None, :], targets[:, 1][None, :])


def empirical_variogram(pts, vals, n_lags=8, max_dist=None):
    """Эмпирическая вариограмма: бины по расстоянию, γ = 0.5·mean(diff²).

    Возвращает (lag_centers, semivars, counts); пустые бины отбрасываются.
    """
    pts = np.asarray(pts, float)
    vals = np.asarray(vals, float)
    ok = np.isfinite(vals)
    pts, vals = pts[ok], vals[ok]
    d = pairwise_km(pts)
    iu = np.triu_indices(len(pts), k=1)
    d_iu = d[iu]
    g = 0.5 * (vals[iu[0]] - vals[iu[1]]) ** 2
    if max_dist is None:
        max_dist = float(d_iu.max()) / 2.0  # классическое правило — половина диагонали
    edges = np.linspace(0.0, max_dist, int(n_lags) + 1)
    centers, semis, counts = [], [], []
    for k in range(int(n_lags)):
        m = (d_iu > edges[k]) & (d_iu <= edges[k + 1])
        if not m.any():
            continue
        centers.append(float(d_iu[m].mean()))
        semis.append(float(g[m].mean()))
        counts.append(int(m.sum()))
    return np.asarray(centers), np.asarray(semis), np.asarray(counts, int)


def fit_variogram(pts, vals, n_lags=8):
    """Подбор nugget/psill/range по эмпирической вариограмме (curve_fit).

    При неуспехе (мало точек, вырожденный биннинг) — консервативный
    фолбэк: nugget=0, psill=var(данных), range=половина максимального лага.
    """
    pts = np.asarray(pts, float)
    vals = np.asarray(vals, float)
    ok = np.isfinite(vals)
    pts, vals = pts[ok], vals[ok]
    v0 = float(np.var(vals)) if len(vals) else 1.0
    v0 = v0 if v0 > 1e-12 else 1.0
    try:
        centers, semis, _ = empirical_variogram(pts, vals, n_lags=n_lags)
        if len(centers) >= 3:
            from scipy.optimize import curve_fit

            p0 = [0.1 * v0, 0.9 * v0, max(float(centers[-1]) / 3.0, 1.0)]
            bounds = ([0.0, 1e-9, 1.0], [np.inf, np.inf, np.inf])
            popt, _ = curve_fit(
                lambda h, c0, s, r: exponential_variogram(h, c0, s, r),
                centers, semis, p0=p0, bounds=bounds, maxfev=20000,
            )
            return Variogram(float(popt[0]), float(popt[1]), float(popt[2]))
    except Exception:
        pass
    d = pairwise_km(pts) if len(pts) > 1 else np.zeros((1, 1))
    return Variogram(0.0, v0, max(float(d.max()) / 2.0, 1.0))


def idw(pts, vals, targets, power=IDW_POWER):
    """Обратные взвешенные расстояния (фолбэк). Совпадение с точкой данных
    даёт её значение точно. Возвращает (pred (M,), var (M,) из NaN)."""
    pts = np.asarray(pts, float)
    vals = np.asarray(vals, float)
    targets = np.atleast_2d(np.asarray(targets, float))
    d = np.maximum(cross_km(pts, targets), 1e-12)
    out = np.empty(len(targets))
    for j in range(len(targets)):
        dj = d[:, j]
        zero = dj < 1e-9
        if zero.any():
            out[j] = float(vals[zero][0])
            continue
        w = 1.0 / dj ** float(power)
        out[j] = float((w * vals).sum() / w.sum())
    return out, np.full(len(targets), np.nan)


def _plane_fit(pts, vals):
    """МНК-плоскость z ~ a + b·lat + c·lon. Возвращает (A, coef)."""
    A = np.column_stack([np.ones(len(pts)), pts[:, 0], pts[:, 1]])
    coef, *_ = np.linalg.lstsq(A, vals, rcond=None)
    return A, coef


def _plane_eval(targets, coef):
    t = np.atleast_2d(np.asarray(targets, float))
    return coef[0] + coef[1] * t[:, 0] + coef[2] * t[:, 1]


@dataclass
class KrigingResult:
    """Результат кринга: прогнозы, дисперсии, метод по каждой цели."""

    pred: np.ndarray        # (M,) прогноз в целевых точках
    var: np.ndarray         # (M,) дисперсия кринга (NaN — для IDW)
    method: np.ndarray      # (M,) строковый флаг "ok"/"idw"
    variogram: Variogram
    weights: np.ndarray     # (M, N+1) веса (последний — множитель Лагранжа)
    n_fallback: int         # сколько целей посчитано через IDW


def krige(pts, vals, targets, variogram=None, detrend=True, n_lags=8, power=IDW_POWER):
    """Обыкновенный кринг значений в целевых точках.

    pts (N,2) lat/lon, vals (N,), targets (M,2). variogram=None → подбирается
    по данным. detrend=True → плоскостный детренд перед крингом.
    Возвращает KrigingResult.
    """
    pts = np.atleast_2d(np.asarray(pts, float))
    vals = np.asarray(vals, float)
    targets = np.atleast_2d(np.asarray(targets, float))
    ok = np.isfinite(vals)
    pts, vals = pts[ok], vals[ok]
    if variogram is None:
        variogram = fit_variogram(pts, vals, n_lags=n_lags)
    if len(pts) < MIN_POINTS:
        p, v = idw(pts, vals, targets, power=power)
        return KrigingResult(p, v, np.array(["idw"] * len(targets)), variogram,
                             np.zeros((len(targets), 0)), len(targets))
    resid = vals
    coef = None
    if detrend:
        A, coef = _plane_fit(pts, vals)
        resid = vals - A @ coef
    d_data = pairwise_km(pts)
    d_cross = cross_km(pts, targets)
    n = len(pts)
    k_mat = np.empty((n + 1, n + 1))
    k_mat[:n, :n] = variogram.cov(d_data)
    k_mat[n, :n] = 1.0
    k_mat[:n, n] = 1.0
    k_mat[n, n] = 0.0
    c_cross = variogram.cov(d_cross)          # (N, M)
    rhs = np.vstack([c_cross, np.ones((1, d_cross.shape[1]))])
    try:
        cond = float(np.linalg.cond(k_mat[:n, :n]))
        if not np.isfinite(cond) or cond > COND_LIMIT:
            raise np.linalg.LinAlgError("ill-conditioned covariance")
        sol = np.linalg.solve(k_mat, rhs)     # (N+1, M)
        w = sol[:n, :]
        lagr = sol[n, :]
        pred = w.T @ resid
        if detrend:
            pred = pred + _plane_eval(targets, coef)
        var = variogram.sill - (w * c_cross).sum(axis=0) - lagr
        var = np.clip(var, 0.0, None)
        method = np.array(["ok"] * len(targets))
        return KrigingResult(pred, var, method, variogram, sol.T, 0)
    except np.linalg.LinAlgError:
        p, v = idw(pts, vals, targets, power=power)
        return KrigingResult(p, v, np.array(["idw"] * len(targets)), variogram,
                             np.zeros((len(targets), 0)), len(targets))
