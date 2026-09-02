import json
from pathlib import Path

import numpy as np
from sklearn.isotonic import IsotonicRegression

from agrocast.backtest.metrics import rps_rows

MIN_CAL_N = 120


class MonoCurve:
    def __init__(self, x=None, y=None):
        self.x = x
        self.y = y

    @classmethod
    def fit(cls, p, target):
        ir = IsotonicRegression(out_of_bounds="clip", y_min=0.02, y_max=0.98)
        ir.fit(np.asarray(p, float), np.asarray(target, float))
        return cls(ir.X_thresholds_, ir.y_thresholds_)

    def __call__(self, p):
        return np.interp(np.asarray(p, float), self.x, self.y, left=self.y[0], right=self.y[-1])


class TercileCalibrator:
    def __init__(self):
        self.curves = None
        self.n = 0

    def fit(self, p, obs):
        p = np.asarray(p, float)
        obs = np.asarray(obs, int)
        self.n = len(obs)
        self.curves = [MonoCurve.fit(p[:, k], (obs == k).astype(float)) for k in range(3)]
        return self

    def transform(self, p):
        p = np.asarray(p, float)
        q = np.column_stack([self.curves[k](p[:, k]) for k in range(3)])
        q = np.clip(q, 0.02, None)
        return q / q.sum(axis=1, keepdims=True)

    def usable(self):
        return self.n >= MIN_CAL_N

    def save(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        data = {"n": self.n, "curves": [{"x": list(c.x), "y": list(c.y)} for c in self.curves]}
        Path(path).write_text(json.dumps(data))

    @classmethod
    def load(cls, path):
        p = Path(path)
        if not p.exists():
            return None
        data = json.loads(p.read_text())
        c = cls()
        c.n = data["n"]
        c.curves = [MonoCurve(np.asarray(d["x"], float), np.asarray(d["y"], float)) for d in data["curves"]]
        return c


def rps_of(p, obs):
    return float(rps_rows(np.asarray(p, float), np.asarray(obs, int)).mean())


def gated_calibrator(p, obs, years=None, hold_years=5, min_hold=48):
    p = np.asarray(p, float)
    obs = np.asarray(obs, int)
    if years is None:
        return TercileCalibrator().fit(p, obs)
    years = np.asarray(years, int)
    cut = int(years.max()) - hold_years
    m_hold = years >= cut
    m_fit = ~m_hold
    if int(m_hold.sum()) < min_hold or int(m_fit.sum()) < MIN_CAL_N:
        return TercileCalibrator().fit(p, obs)
    cal = TercileCalibrator().fit(p[m_fit], obs[m_fit])
    if not cal.usable():
        return cal
    if rps_of(cal.transform(p[m_hold]), obs[m_hold]) < rps_of(p[m_hold], obs[m_hold]):
        return cal
    return TercileCalibrator()
