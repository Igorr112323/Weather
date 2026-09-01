import numpy as np

from agrocast.core.mathutils import weighted_quantile, weighted_tercile_probs
from agrocast.models.base import ForecastModel, select_features


class AnalogModel(ForecastModel):
    name = "analog"

    def __init__(self, k=10):
        self.k = int(k)

    def fit(self, X, y, w=None, edges=None, years=None):
        self.cols = select_features(X, y)
        A = X[self.cols].to_numpy(float)
        self.mu = A.mean(axis=0)
        self.sd = A.std(axis=0) + 1e-9
        self.A = (A - self.mu) / self.sd
        self.y = np.asarray(y, float)
        self.years = None if years is None else np.asarray(years, int)
        return self

    def _neighbors(self, x):
        v = (np.asarray(x[self.cols], float) - self.mu) / self.sd
        d = ((self.A - v) ** 2).mean(axis=1)
        k = min(self.k, len(d))
        idx = np.argsort(d)[:k]
        scale = max(float(d[idx].mean()), 1e-9)
        w = np.exp(-d[idx] / scale)
        return idx, w, d[idx]

    def predict(self, x, e1, e2):
        idx, w, _ = self._neighbors(x)
        yn = self.y[idx]
        p = weighted_tercile_probs(yn, e1, e2, w)
        p = 0.65 * p + 0.35 * np.full(3, 1.0 / 3.0)
        q = weighted_quantile(yn, [0.1, 0.5, 0.9], w)
        return p, np.asarray(q)

    def analogs(self, x, k=8):
        idx, w, d = self._neighbors(x)
        order = np.argsort(d)[:k]
        out = []
        for j in order:
            year = None if self.years is None else int(self.years[idx[j]])
            out.append({"index": int(idx[j]), "year": year, "z": float(self.y[idx[j]]), "weight": float(w[j] / w.sum())})
        return out
