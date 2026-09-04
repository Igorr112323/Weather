# -*- coding: utf-8 -*-
"""Пространственный слой «регион»: сетка ячеек КРА + обыкновенный кринг.

Модули:
- grid: построение сетки ячеек 0.5° по Краснодарскому краю с фильтром
  покрытия данных (артефакт world/artifacts/krai_grid.json);
- kriging: обыкновенный кринг с экспоненциальной вариограммой,
  детрендингом плоскостью и IDW-фолбэком.
"""
from agrocast.region.grid import (
    KRA_BOUNDS,
    CELL_DEG,
    MIN_COVERAGE,
    cell_centers,
    coverage_from_daily,
    build_grid,
    load_grid,
    grid_points,
)
from agrocast.region.kriging import (
    Variogram,
    exponential_variogram,
    empirical_variogram,
    fit_variogram,
    krige,
    idw,
)

__all__ = [
    "KRA_BOUNDS", "CELL_DEG", "MIN_COVERAGE",
    "cell_centers", "coverage_from_daily", "build_grid", "load_grid", "grid_points",
    "Variogram", "exponential_variogram", "empirical_variogram", "fit_variogram",
    "krige", "idw",
]
