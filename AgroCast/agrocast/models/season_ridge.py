import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge

from agrocast.core.mathutils import monotonize_q, tercile_probs_normal
from agrocast.models.base import ForecastModel
from agrocast.models.clim import ClimModel

Z90 = 1.28155

DJF_FEAT = ["sstfc_pdo_f3", "sstfc_pdo_f1", "snow_a", "scand_3m"]
MAM_FEAT = ["sstfc_pdo_f3", "sstfc_pdo_f1", "ls_w3", "nino34_3m_s6"]

SEASON_SPEC = {
    "DJF": ((12, 1, 2), DJF_FEAT),
    "MAM": ((3, 4, 5), MAM_FEAT),
}


class SeasonRidge(ForecastModel):
    def __init__(self, name, months, features):
        self.name = name
        if isinstance(months, (int, np.integer)):
            months = (int(months),)
        self.season_months = tuple(int(m) for m in months)
        self.features = list(features)
        self.clim = ClimModel()
        self._ridge = None

    def fit(self, X, y, w=None, edges=None, years=None):
        tmonth = X["tmonth"].to_numpy(float)
        mask = pd.Series(np.isin(tmonth, np.asarray(self.season_months, float)), index=X.index)
        self.clim.fit(X, y, w=w, edges=edges, years=years)
        if not mask.any():
            self._ridge = None
            return self
        try:
            Xm = X.loc[mask, self.features].to_numpy(float)
        except Exception:
            self._ridge = None
            return self
        ym = np.asarray(y, float)[mask.to_numpy()]
        keep = np.isfinite(Xm).all(axis=1) & np.isfinite(ym)
        if keep.sum() < 12:
            self._ridge = None
            return self
        self._ridge = Ridge(alpha=3.0).fit(Xm[keep], ym[keep])
        return self

    def predict(self, x, e1, e2):
        tm = self._val(x, "tmonth")
        if self._ridge is None or not np.isin(tm, np.asarray(self.season_months, float)).any():
            return self.clim.predict(x, e1, e2)
        vals = np.array([self._val(x, f) for f in self.features])
        if not np.isfinite(vals).all():
            return self.clim.predict(x, e1, e2)
        mu = float(self._ridge.predict(vals.reshape(1, -1))[0])
        mu = float(np.clip(mu, -1.5, 1.5))
        probs = tercile_probs_normal(mu, 1.0, e1, e2)
        q = mu + np.array([-Z90, 0.0, Z90])
        return probs, monotonize_q(q)

    @staticmethod
    def _val(x, name):
        try:
            return float(x[name])
        except Exception:
            return np.nan
