import numpy as np

from agrocast.core.mathutils import tercile_probs_normal, monotonize_q, clip_probs
from agrocast.models.base import ForecastModel, select_features

try:
    from lightgbm import LGBMRegressor, LGBMClassifier

    HAS_LGBM = True
except ImportError:
    from sklearn.ensemble import GradientBoostingRegressor, GradientBoostingClassifier

    HAS_LGBM = False

Z90 = 1.2815515655446004


def _quantile_model(alpha, seed):
    if HAS_LGBM:
        return LGBMRegressor(
            objective="quantile",
            alpha=alpha,
            n_estimators=120,
            learning_rate=0.03,
            num_leaves=7,
            min_child_samples=10,
            subsample=0.8,
            subsample_freq=1,
            colsample_bytree=0.8,
            reg_lambda=5.0,
            random_state=seed,
            n_jobs=1,
            verbose=-1,
        )
    return GradientBoostingRegressor(
        loss="quantile",
        alpha=alpha,
        n_estimators=50,
        max_depth=2,
        learning_rate=0.05,
        random_state=seed,
    )


def _classifier(seed):
    if HAS_LGBM:
        return LGBMClassifier(
            n_estimators=120,
            learning_rate=0.03,
            num_leaves=7,
            min_child_samples=10,
            subsample=0.8,
            subsample_freq=1,
            colsample_bytree=0.8,
            reg_lambda=5.0,
            random_state=seed,
            n_jobs=1,
            verbose=-1,
        )
    return GradientBoostingClassifier(n_estimators=50, max_depth=2, learning_rate=0.05, random_state=seed)


class GBMModel(ForecastModel):
    name = "gbm"

    def __init__(self, seed=0):
        self.seed = seed

    def fit(self, X, y, w=None, edges=None, years=None):
        self.cols = select_features(X, y)
        Xf = X[self.cols]
        yv = np.asarray(y, float)
        sw = None if w is None else np.asarray(w, float)
        self.q_models = {}
        for alpha in (0.1, 0.5, 0.9):
            m = _quantile_model(alpha, self.seed)
            m.fit(Xf, yv, sample_weight=sw)
            self.q_models[alpha] = m
        self.clf = None
        self.const_probs = None
        if edges is not None:
            e1 = np.asarray(edges, float)[:, 0]
            e2 = np.asarray(edges, float)[:, 1]
            lab = np.where(yv < e1, 0, np.where(yv <= e2, 1, 2))
            if len(np.unique(lab)) >= 2:
                try:
                    c = _classifier(self.seed)
                    c.fit(Xf, lab, sample_weight=sw)
                    self.clf = c
                except Exception:
                    self.clf = None
            else:
                u, cnt = np.unique(lab, return_counts=True)
                p = np.full(3, 0.02)
                p[u] = cnt / cnt.sum()
                self.const_probs = clip_probs(p)
        return self

    def predict(self, x, e1, e2):
        xf = x.to_frame().T[self.cols]
        q = np.array([float(self.q_models[a].predict(xf)[0]) for a in (0.1, 0.5, 0.9)])
        q = monotonize_q(q)
        if self.const_probs is not None:
            return self.const_probs, q
        if self.clf is None:
            sd = max((q[2] - q[0]) / (2 * Z90), 0.15)
            return tercile_probs_normal(q[1], sd, e1, e2), q
        proba = self.clf.predict_proba(xf)[0]
        classes = list(self.clf.classes_)
        p = np.full(3, 0.02)
        for i, c in enumerate(classes):
            p[int(c)] = proba[i]
        p = clip_probs(p)
        p = 0.7 * p + 0.3 * np.full(3, 1.0 / 3.0)
        return clip_probs(p), q
