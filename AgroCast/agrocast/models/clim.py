import numpy as np

from agrocast.core.mathutils import weighted_quantile, weighted_tercile_probs
from agrocast.models.base import ForecastModel


class ClimModel(ForecastModel):
    name = "clim"

    def fit(self, X, y, w=None, edges=None, years=None):
        self.y = np.asarray(y, float)
        self.w = None if w is None else np.asarray(w, float)
        return self

    def predict(self, x, e1, e2):
        p = weighted_tercile_probs(self.y, e1, e2, self.w)
        p = 0.7 * p + 0.3 * np.full(3, 1.0 / 3.0)
        q = weighted_quantile(self.y, [0.1, 0.5, 0.9], self.w)
        return p, np.asarray(q)

    def analogs(self, x, k=8):
        order = np.argsort(-self.y)[:k]
        return [(int(i), float(self.y[i])) for i in order]
