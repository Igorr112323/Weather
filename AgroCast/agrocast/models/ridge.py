import numpy as np
from sklearn.linear_model import Ridge

from agrocast.core.mathutils import tercile_probs_normal, monotonize_q
from agrocast.models.base import ForecastModel, select_features

Z90 = 1.2815515655446004


class RidgeModel(ForecastModel):
    name = "ridge"

    def __init__(self, alpha=3.0):
        self.alpha = alpha

    def fit(self, X, y, w=None, edges=None, years=None):
        self.cols = select_features(X, y)
        Xm = X[self.cols].to_numpy(float)
        self.mu = Xm.mean(axis=0)
        self.sd = Xm.std(axis=0) + 1e-9
        self.sd = np.where(self.sd < 0.05, 1.0, self.sd)
        Z = (Xm - self.mu) / self.sd
        yv = np.asarray(y, float)
        sw = None if w is None else np.asarray(w, float)
        self.ridge = Ridge(alpha=self.alpha)
        self.ridge.fit(Z, yv, sample_weight=sw)
        loo = []
        n = len(yv)
        for i in range(n):
            m = np.ones(n, bool)
            m[i] = False
            try:
                ri = Ridge(alpha=self.alpha).fit(Z[m], yv[m], sample_weight=None if sw is None else sw[m])
                loo.append(float(yv[i] - ri.predict(Z[i : i + 1])[0]))
            except Exception:
                loo.append(float(yv[i] - self.ridge.predict(Z[i : i + 1])[0]))
        rsd = float(np.std(loo)) if n > 3 else 1.0
        self.rsd = float(np.clip(rsd, 0.35, 1.3))
        return self

    def predict(self, x, e1, e2):
        xv = (np.asarray(x[self.cols].to_numpy() if hasattr(x, "index") else x, float) - self.mu) / self.sd
        mu = float(self.ridge.predict(xv.reshape(1, -1))[0])
        mu = float(np.clip(mu, -1.5, 1.5))
        probs = tercile_probs_normal(mu, self.rsd, e1, e2)
        q = mu + np.array([-Z90, 0.0, Z90]) * self.rsd
        return probs, monotonize_q(q)
