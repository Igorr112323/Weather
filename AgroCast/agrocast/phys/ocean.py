import numpy as np
import pandas as pd

BOXES = {
    "nino34": (-5.0, 5.0, 190.0, 240.0),
    "iod": (-30.0, 0.0, 50.0, 70.0),
    "amo": (0.0, 45.0, 260.0, 340.0),
    "nao": (40.0, 65.0, 340.0, 361.0),
    "pdo": (20.0, 45.0, 120.0, 140.0),
}


def box_series(sst, lat0, lat1, lon0, lon1):
    sel = sst.sel(lat=slice(lat0, lat1), lon=slice(lon0, min(lon1, float(sst.lon.values.max()))))
    return sel.mean(dim=["lat", "lon"]).to_series()


class TwoLayerSST:
    def __init__(self):
        self.r = None
        self.sig = 0.0

    def fit(self, t1, fit_end=None):
        a = self.anom_all(t1)
        if fit_end is not None:
            a = a[a.index.year <= fit_end]
        if len(a) < 120:
            return False
        x = a.to_numpy(float)
        xa = np.stack([x[:-2], x[1:-1]], axis=1)
        y = x[2:]
        coef, _, _, _ = np.linalg.lstsq(xa, y, rcond=None)
        r1, r2 = float(coef[0]), float(coef[1])
        A = np.array([[r1, r2], [1.0, 0.0]])
        w = np.linalg.eigvals(A)
        lam = float(np.max(np.abs(w)))
        if lam > 0.999:
            A = A * 0.999 / lam
        self.r = (float(A[0, 0]), float(A[0, 1]), float(A[1, 0]), float(A[1, 1]))
        pred = xa @ np.array([A[0, 0], A[0, 1]])
        resid = (pred - y).astype(float)
        self.sig = float(np.std(resid))
        return True

    def anom_all(self, t1):
        if not isinstance(t1.index, pd.PeriodIndex):
            t1 = t1.copy()
            t1.index = pd.PeriodIndex(t1.index, freq="M")
        base = t1.groupby(t1.index.month).transform("mean")
        return (t1 - base).dropna()

    def forecast(self, t1, issue, leads, n_members=20, seed=0):
        a = self.anom_all(t1)
        a = a[a.index < issue]
        if len(a) < 12 or self.r is None:
            return None
        x = np.array([float(a.iloc[-1]), float(a.iloc[-2])])
        rng = np.random.default_rng(seed)
        A = np.array([[self.r[0], self.r[1]], [self.r[2], self.r[3]]])
        vs = np.tile(x, (n_members, 1))
        out = np.zeros((n_members, len(leads)))
        out[:, 0] = vs[:, 0]
        for h in range(1, len(leads)):
            vs = vs @ A.T + rng.normal(0.0, self.sig, (n_members, 1))
            out[:, h] = vs[:, 0]
        return {"mean": out.mean(axis=0), "spread": out.std(axis=0)}


class DelayedSST:
    def __init__(self, tau=18):
        self.tau = tau
        self.c1 = None
        self.c2 = None
        self.sig = 0.0

    def fit(self, t1, fit_end=None):
        a = self.anom_all(t1)
        if fit_end is not None:
            a = a[a.index.year <= fit_end]
        if len(a) < self.tau + 120:
            return False
        x = a.to_numpy(float)
        L = len(x)
        y = x[self.tau + 1:]
        X = np.stack([x[self.tau:self.tau + len(y)], x[:len(y)]], axis=1)
        coef, _, _, _ = np.linalg.lstsq(X, y, rcond=None)
        c1, c2 = float(coef[0]), float(coef[1])
        r = np.roots([1.0, -c1, -c2])
        lam = float(np.max(np.abs(r)))
        if lam > 0.999:
            c1 *= 0.999 / lam
            c2 *= 0.999 / lam
        self.c1 = c1
        self.c2 = c2
        self.sig = float(np.std(y - X @ np.array([c1, c2])))
        return True

    def anom_all(self, t1):
        if not isinstance(t1.index, pd.PeriodIndex):
            t1 = t1.copy()
            t1.index = pd.PeriodIndex(t1.index, freq="M")
        base = t1.groupby(t1.index.month).transform("mean")
        return (t1 - base).dropna()

    def forecast(self, t1, issue, leads, n_members=20, seed=0):
        a = self.anom_all(t1)
        a = a[a.index < issue]
        if self.c1 is None or len(a) < self.tau + 12:
            return None
        h = a.to_numpy(float)
        out = np.zeros((n_members, len(leads)))
        for m in range(n_members):
            rng = np.random.default_rng(seed + m)
            for k, ld in enumerate(leads):
                cur = list(h)
                for j in range(len(h) - self.tau, len(h)):
                    cur[j] += rng.normal(0.0, 0.25)
                for _ in range(ld):
                    cur.append(self.c1 * cur[-1] + self.c2 * cur[-1 - self.tau] + rng.normal(0.0, self.sig))
                out[m, k] = cur[-1]
        return {"mean": out.mean(axis=0), "spread": out.std(axis=0)}
