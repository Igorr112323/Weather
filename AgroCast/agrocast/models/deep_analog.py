"""Аналог в пространстве «глубинного состояния» предыдущего сезона.

Обычный аналог ищет похожие месяцы по одной точке предикторного кадра
(топ-k признаков, выбранных по корреляции). Здесь ищется аналог *целого
предыдущего состояния*: совокупный «след» зимы к дате выпуска прогноза —
полярный вихрь (u10, z50), снежный покров, почвенная влага, локальные
температура и осадки, SST Чёрного/Средиземного морей, ЭНСКО (PC1).

Расстояние считается в пространстве первых главных компонент (белое
пространство ПСК), а не евклидово по признакам — это учитывает
корреляции между драйверами и не даёт одному сильному признаку
«задавить» остальные.

Источник подхода: метод аналогов в пространстве сингулярных значений —
классика субсезонной и сезонной предсказуемости (Mears & Calvert 1987;
Anet et al. 2017), здесь применён к «глубинному» (мульти-месячному)
состоянию, что и есть заявленная новизна для агро-сезона.
"""
import numpy as np

from agrocast.core.mathutils import weighted_quantile, weighted_tercile_probs
from agrocast.models.base import ForecastModel

# Ручной набор признаков «глубинного состояния» (какие есть в кадре)
DEEP_COLS = [
    "u10_a", "u10_a_l1", "u10_a_l3", "z50_a", "z50_a_l3",
    "snow_a", "swvl_a", "t2m_a", "tp_a",
    "black_a", "black_a_s3", "med_a", "med_a_s3",
    "pc1", "pc1_l3", "nino34_3m",
]

SHRINK = 0.35  # сжатие к климатологическим терцилям


class DeepAnalogModel(ForecastModel):
    name = "deep_analog"

    def __init__(self, k=12, n_pcs=5):
        self.k = int(k)
        self.n_pcs = int(n_pcs)

    def _cols(self, X):
        return [c for c in DEEP_COLS if c in X.columns]

    def fit(self, X, y, w=None, edges=None, years=None):
        self.cols = self._cols(X)
        A = X[self.cols].to_numpy(float)
        self.mu = A.mean(axis=0)
        self.sd = A.std(axis=0) + 1e-9
        self.sd = np.where(self.sd < 0.05, 1.0, self.sd)
        Z = (A - self.mu) / self.sd
        Zc = Z - Z.mean(axis=0)
        U, S, Vt = np.linalg.svd(Zc, full_matrices=False)
        self.n_comp = min(self.n_pcs, len(S), Zc.shape[1])
        self.P = Vt[: self.n_comp]
        self.p_sd = S[: self.n_comp] / np.sqrt(max(len(Z) - 1, 1))
        self.p_sd = np.maximum(self.p_sd, 1e-9)
        self.PM = (Z @ self.P.T) / self.p_sd
        self.y = np.asarray(y, float)
        self.years = None if years is None else np.asarray(years, int)
        return self

    def _proj(self, v):
        return (np.asarray(v, float) @ self.P.T) / self.p_sd

    def _neighbors(self, x):
        v = (np.asarray(x[self.cols], float) - self.mu) / self.sd
        pv = self._proj(v)
        d = ((self.PM - pv) ** 2).sum(axis=1) / self.n_comp
        k = min(self.k, len(d))
        idx = np.argsort(d)[:k]
        scale = max(float(d[idx].mean()), 1e-9)
        wgt = np.exp(-d[idx] / scale)
        return idx, wgt, d[idx]

    def predict(self, x, e1, e2):
        if len(self.cols) < 4:
            raise RuntimeError("deep_analog: недостаточно признаков состояния")
        idx, wgt, _ = self._neighbors(x)
        yn = self.y[idx]
        p = weighted_tercile_probs(yn, e1, e2, wgt)
        p = (1.0 - SHRINK) * p + SHRINK * np.full(3, 1.0 / 3.0)
        q = weighted_quantile(yn, [0.1, 0.5, 0.9], wgt)
        return p, np.asarray(q)

    def analogs(self, x, k=8):
        idx, wgt, d = self._neighbors(x)
        order = np.argsort(d)[:k]
        out = []
        for j in order:
            year = None if self.years is None else int(self.years[idx[j]])
            out.append(
                {
                    "index": int(idx[j]),
                    "year": year,
                    "z": float(self.y[idx[j]]),
                    "weight": float(wgt[j] / wgt.sum()),
                }
            )
        return out
