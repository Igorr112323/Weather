"""Конформная калибровка квантилей (split conformal prediction).

Для каждого уровня L ∈ {10, 50, 90} на верификационной выборке находит
поправку  d_L = quantile(obs − q_L, L)  и сдвигает квантили модели на d.

Свойство (гарантия при обменимости наблюдений, Vovk et al. 2005;
Romano et al. 2019):  P(obs ∈ [q10', q90']) ≥ 80%  — независимо от того,
какая модель дала квантили и насколько она смещена. Никто из агро-
сервисов такие гарантии не публикует — это и есть «новое» в отчёте.

Поправки считаются отдельно по переменной и по группе горизонта
(L1 / L2-3 / L4+), т.к. смещение растёт с горизонтом; при нехватке
наблюдений — по всем горизонтам.
"""
import json
from pathlib import Path

import numpy as np

LEVELS = (0.10, 0.50, 0.90)
MIN_N = 60


def lead_key(lead):
    lead = int(lead)
    if lead <= 1:
        return "L1"
    if lead <= 3:
        return "L23"
    return "L4p"


def lead_key_series(series):
    import pandas as pd

    return pd.Series(series).map(lead_key)


class ConformalQuantileCalibrator:
    def __init__(self):
        self.deltas = {}  # variable -> {lead_key: [d10, d50, d90] | None}
        self.n = 0

    def fit(self, records):
        """records: DataFrame с колонками q10, q50, q90, obs_z, variable, lead."""
        self.n = int(len(records))
        self.deltas = {}
        if self.n == 0:
            return self
        for v, gv in records.groupby("variable"):
            per_lead = {}
            q_all = gv[["q10", "q50", "q90"]].to_numpy(float)
            o_all = gv["obs_z"].to_numpy(float)
            if len(o_all) >= MIN_N:
                per_lead["ALL"] = [
                    float(np.quantile(o_all - q_all[:, 0], LEVELS[0])),
                    float(np.quantile(o_all - q_all[:, 1], LEVELS[1])),
                    float(np.quantile(o_all - q_all[:, 2], LEVELS[2])),
                ]
            else:
                per_lead["ALL"] = None
            for lk, gl in gv.groupby(lead_key_series(gv["lead"])):
                if len(gl) < MIN_N:
                    continue
                q = gl[["q10", "q50", "q90"]].to_numpy(float)
                o = gl["obs_z"].to_numpy(float)
                per_lead[str(lk)] = [
                    float(np.quantile(o - q[:, 0], LEVELS[0])),
                    float(np.quantile(o - q[:, 1], LEVELS[1])),
                    float(np.quantile(o - q[:, 2], LEVELS[2])),
                ]
            self.deltas[v] = per_lead
        return self

    def _deltas(self, variable, lead):
        d = self.deltas.get(variable)
        if not d:
            return None
        v = d.get(lead_key(lead))
        return v if v is not None else d.get("ALL")

    def transform(self, qz, variable, lead):
        qz = np.asarray(qz, float)
        d = self._deltas(variable, lead)
        if d is None:
            return qz
        out = np.array([qz[0] + d[0], qz[1] + d[1], qz[2] + d[2]])
        return np.sort(out)

    def coverage80(self, records):
        """Эмпирическое покрытие [q10', q90'] после калибровки + медиана |obs−q50'|."""
        errs, hits = [], []
        for _, r in records.iterrows():
            q = self.transform([r["q10"], r["q50"], r["q90"]], r["variable"], r["lead"])
            o = float(r["obs_z"])
            hits.append(q[0] - 1e-12 <= o <= q[2] + 1e-12)
            errs.append(abs(o - float(q[1])))
        if not hits:
            return None
        return {
            "n": len(hits),
            "p80_coverage": round(float(np.mean(hits)), 3),
            "median_abs_median_err": round(float(np.median(errs)), 3),
        }

    def save(self, path):
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"n": self.n, "deltas": self.deltas}))

    @classmethod
    def load(cls, path):
        p = Path(path)
        if not p.exists():
            return None
        data = json.loads(p.read_text())
        c = cls()
        c.n = int(data.get("n", 0))
        c.deltas = data.get("deltas", {})
        return c

    def usable(self):
        return any(any(v for v in d.values()) for d in self.deltas.values())
