"""Конформная калибровка квантилей (split-conformal стиль, quantile shifts).

Для каждого уровня L ∈ {10, 50, 90} на верификационной выборке находится
поправка d_L = quantile(obs − q_L, L) и сдвигает квантили модели на d.

Поправка для конечной выборки (n наблюдений): нижний уровень берётся
индексом floor((n+1)L)−1 (сдвиг влево), верхний — ceil((n+1)L)−1
(сдвиг вправо), что расширяет интервал при малом n. Предпосылка
корректности — обменимость пар (score, точка проверки); в прогнозном
временном ряду с перекрывающимися сезонными окнами она выполняется лишь
приближённо, поэтому интервалы публикуются без обещания безусловного
покрытия: гарантия условная, эмпирическое покрытие измеряется только на
выборке вне fit (см. coverage80), с биномиальным CI и шириной.

Поправки считаются отдельно по переменной и по группе горизонта
(L1 / L2-3 / L4+), т.к. смещение растёт с горизонтом; при нехватке
наблюдений — по всем горизонтам.
"""

import numpy as np

LEVELS = (0.10, 0.50, 0.90)
MIN_N = 60


def _fs_quantile(x, level, side):
    import math

    x = np.sort(np.asarray(x, float))
    n = len(x)
    if n == 0:
        return float("nan")
    if n == 1:
        return float(x[0])
    if side == "low":
        idx = max(0, int(math.floor((n + 1) * float(level))) - 1)
    elif side == "high":
        idx = min(n - 1, int(math.ceil((n + 1) * float(level))) - 1)
    else:
        return float(np.quantile(x, float(level)))
    return float(x[idx])


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
        self.fit_years = {}  # variable -> [target year, ...] fingerprints of fit rows

    def fit(self, records):
        """records: DataFrame с колонками q10, q50, q90, obs_z, variable, lead."""
        self.n = int(len(records))
        self.deltas = {}
        self.fit_years = {}
        if len(records) and "year" in records.columns:
            for v, gv in records.groupby("variable"):
                self.fit_years[str(v)] = sorted(int(y) for y in gv["year"].unique())
        if self.n == 0:
            return self
        for v, gv in records.groupby("variable"):
            per_lead = {}
            q_all = gv[["q10", "q50", "q90"]].to_numpy(float)
            o_all = gv["obs_z"].to_numpy(float)
            if len(o_all) >= MIN_N:
                per_lead["ALL"] = [
                    _fs_quantile(o_all - q_all[:, 0], LEVELS[0], "low"),
                    _fs_quantile(o_all - q_all[:, 1], LEVELS[1], "mid"),
                    _fs_quantile(o_all - q_all[:, 2], LEVELS[2], "high"),
                ]
            else:
                per_lead["ALL"] = None
            for lk, gl in gv.groupby(lead_key_series(gv["lead"])):
                if len(gl) < MIN_N:
                    continue
                q = gl[["q10", "q50", "q90"]].to_numpy(float)
                o = gl["obs_z"].to_numpy(float)
                per_lead[str(lk)] = [
                    _fs_quantile(o - q[:, 0], LEVELS[0], "low"),
                    _fs_quantile(o - q[:, 1], LEVELS[1], "mid"),
                    _fs_quantile(o - q[:, 2], LEVELS[2], "high"),
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

    def coverage80(self, records, require_out_of_fit=True):
        """Эмпирическое покрытие [q10', q90'] вне fit-выборки + CI, ширина, ошибка медианы."""
        overlap = 0
        if require_out_of_fit and self.fit_years and "year" in records.columns:
            mask = np.zeros(len(records), bool)
            for v, ys in self.fit_years.items():
                mask |= (records["variable"].astype(str) == v).to_numpy() & records["year"].isin(ys).to_numpy()
            overlap = int(mask.sum())
            if overlap:
                raise ValueError(
                    f"coverage измеряется только вне fit/calibration: {overlap} строк попали в fit-окно conformal"
                )
        errs, hits, widths = [], [], []
        for _, r in records.iterrows():
            q = self.transform([r["q10"], r["q50"], r["q90"]], r["variable"], r["lead"])
            o = float(r["obs_z"])
            hits.append(q[0] - 1e-12 <= o <= q[2] + 1e-12)
            errs.append(abs(o - float(q[1])))
            widths.append(float(q[2] - q[0]))
        if not hits:
            return None
        from agrocast.backtest.metrics import wilson_interval

        k = int(np.sum(hits))
        lo, hi = wilson_interval(k, len(hits))
        return {
            "n": len(hits),
            "p80_coverage": round(float(np.mean(hits)), 3),
            "coverage_wilson95": [round(lo, 4), round(hi, 4)],
            "mean_width_z": round(float(np.mean(widths)), 3),
            "median_abs_median_err": round(float(np.median(errs)), 3),
            "finite_sample_correction": True,
            "note": "покрытие без обещания безусловных 80%: гарантия условна при обменимости",
        }

    def save(self, path):
        from agrocast.core.artifacts import CONFORMAL_SCHEMA, write_artifact

        write_artifact(path, {"n": self.n, "deltas": self.deltas, "fit_years": self.fit_years}, CONFORMAL_SCHEMA)

    @classmethod
    def load(cls, path):
        from agrocast.core.artifacts import CONFORMAL_SCHEMA, read_artifact

        data = read_artifact(path, schema=CONFORMAL_SCHEMA, name="conformal calibration")
        if data is None:
            return None
        c = cls()
        c.n = int(data.get("n", 0))
        c.deltas = data.get("deltas", {})
        c.fit_years = {str(k): [int(x) for x in v] for k, v in (data.get("fit_years") or {}).items()}
        return c

    def usable(self):
        return any(any(v for v in d.values()) for d in self.deltas.values())
