import numpy as np
from sklearn.neural_network import MLPClassifier

HIDDEN = 4
ALPHA = 30.0
SEEDS = (0, 1, 2, 3, 4)
MIN_ROWS = 60


class PooledNN:
    def __init__(self, hidden=HIDDEN, alpha=ALPHA, seeds=SEEDS):
        self.hidden = hidden
        self.alpha = alpha
        self.seeds = seeds
        self.mu = None
        self.sd = None
        self.nets = []
        self.n = 0

    def fit(self, pf, std, pool, mode, max_target_year, first_year=1995):
        cols = [c for c in pool if c in pf.columns]
        X, y = [], []
        for tgt in std.index:
            if tgt.year < first_year or tgt.year >= max_target_year:
                continue
            for lead in ([1] if mode == "seasonal" else range(1, 7)):
                issue = tgt - lead
                if issue not in pf.index:
                    continue
                r = pf.loc[issue, cols]
                if not np.isfinite(r.to_numpy(float)).all():
                    continue
                z, e1, e2 = float(std.loc[tgt, "z"]), float(std.loc[tgt, "e1"]), float(std.loc[tgt, "e2"])
                if not np.isfinite(z):
                    continue
                cls = 0 if z < e1 else (2 if z > e2 else 1)
                X.append(list(r.to_numpy(float)) + [np.sin(2 * np.pi * tgt.month / 12), np.cos(2 * np.pi * tgt.month / 12), lead / 12.0])
                y.append(cls)
        y = np.array(y, int)
        self.n = len(y)
        if self.n < MIN_ROWS or len(np.unique(y)) < 2:
            return self
        X = np.vstack(X)
        self.mu = X.mean(axis=0)
        self.sd = X.std(axis=0) + 1e-9
        Z = (X - self.mu) / self.sd
        self.nets = []
        for s in self.seeds:
            m = MLPClassifier(hidden_layer_sizes=(self.hidden,), alpha=self.alpha, max_iter=500, random_state=s)
            m.fit(Z, y)
            self.nets.append(m)
        return self

    def usable(self):
        return len(self.nets) > 0

    def probs_for(self, pf, issue, pool, tgt_month, lead):
        if not self.usable():
            return None
        cols = [c for c in pool if c in pf.columns]
        r = pf.loc[issue, cols]
        if not np.isfinite(r.to_numpy(float)).all():
            return None
        x = np.array(list(r.to_numpy(float)) + [np.sin(2 * np.pi * tgt_month / 12), np.cos(2 * np.pi * tgt_month / 12), lead / 12.0], float).reshape(1, -1)
        z = (x - self.mu) / self.sd
        P = np.zeros(3)
        for m in self.nets:
            pr = np.zeros(3)
            pr[m.classes_] = m.predict_proba(z)[0]
            P += pr
        return P / len(self.nets)
