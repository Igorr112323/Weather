import numpy as np
import pandas as pd


def select_features(X, y, k=None):
    cols = [c for c in X.columns if c != "lead"]
    if k is None:
        k = max(3, min(8, len(y) // 6))
    if len(cols) <= k:
        return list(X.columns)
    cors = X[cols].corrwith(pd.Series(np.asarray(y, float), index=X.index)).abs()
    keep = list(cors.sort_values(ascending=False).head(k).index)
    return keep + ["lead"]


class ForecastModel:
    name = "base"

    def fit(self, X, y, w=None, edges=None, years=None):
        raise NotImplementedError

    def predict(self, x, e1, e2):
        raise NotImplementedError

    def analogs(self, x, k=8):
        return []
