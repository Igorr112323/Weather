import re

from agrocast.models.base import ForecastModel
from agrocast.models.ridge import RidgeModel

LAND_PATTERN = r"^(swvl_a|snow_a|t2m_a|tp_a|swvl|snow)$"
STRAT_PATTERN = r"^(u10|z50)_a(_l[0-9]+|_s[0-9]+)?$"
OCEAN_PATTERN = r"^(pc[123](_l[0-9]+|_s[0-9]+|_f[0-9]+|_e)?|med_a(_s3)?|black_a(_s3)?)$"


class WindowedRidge(ForecastModel):
    def __init__(self, name, pattern, alpha=3.0):
        self.name = name
        self.pattern = re.compile(pattern)
        self.alpha = alpha

    def _keep(self, obj):
        if hasattr(obj, "columns"):
            cols = [c for c in obj.columns if self.pattern.match(c) or c == "lead"]
            return obj[cols]
        cols = [c for c in obj.index if self.pattern.match(c) or c == "lead"]
        return obj[cols]

    def fit(self, X, y, w=None, edges=None, years=None):
        self.inner = RidgeModel(alpha=self.alpha)
        self.inner.fit(self._keep(X), y, w=w, edges=edges, years=years)
        return self

    def predict(self, x, e1, e2):
        return self.inner.predict(self._keep(x), e1, e2)
