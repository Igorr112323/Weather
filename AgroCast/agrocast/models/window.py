import re

from agrocast.models.base import ForecastModel
from agrocast.models.ridge import RidgeModel

LAND_PATTERN = r"^(swvl_a|snow_a|t2m_a|tp_a|swvl|snow|ls_(swe|melt_idx|snowcov|frozen_cm|laifrac|w[1-4]|t[1-4])|lsf_(swe|w1|t1)_f[12]|lss_sp_(swe|w1|t1)_f3)$"
STRAT_PATTERN = r"^(u10|z50)_a(_l[0-9]+|_s[0-9]+)?$"
OCEAN_PATTERN = r"^(pc[123](_l[0-9]+|_s[0-9]+|_f[0-9]+|_e)?|med_a(_s3)?|black_a(_s3)?|nino34(_1m|_3m|_3m_s6|_3m_e)?|nao(_1m|_3m)?|soi(_1m|_3m)?|amo(_1m|_3m)?|pdo(_1m|_3m)?|scand(_1m|_3m)?|sstfc_(nino34|iod|amo|nao|pdo)_f[13])$"


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
