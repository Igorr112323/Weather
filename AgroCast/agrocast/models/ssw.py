"""Стратосферный триггер: резкие стратосферные потепления (SSW).

SSW — коллапс полярного вихря: зональный ветер на 10 гПа (u10) переворачивается
с западного на восточный. Это один из немногих подтверждённых источников
субсезонной/сезонной предсказуемости в северном полушарии: после SSW вихрь
остаётся ослабленным 4–8 недель, а над умеренными широтами чаще наступает
тёплая, более влажная весна и позже — выше влажность почв к посевной
(Pousani et al. 2015; Kretschmer et al. 2018; Barnes 2018).

Модель намеренно простая и проверяемая: условные частоты терцилей цели
при «вихрь ослаблен в последние 1–2 месяца» против «не ослаблен»,
со сглаживанием и сжатием к климату. Если на backtest условный навык
не подтвердится — блэнд по RPSS сам задаст модели вес ~0, система не
проиграет.

Данные: собственный стратосферный ряд u10 (10 гПа) из открытых
реанализов (world/zarr/strat_snow), чужих прогнозов нет.
"""
import numpy as np

from agrocast.core.mathutils import clip_probs
from agrocast.models.base import ForecastModel

U10_COL = "u10_a"
U10_LAG = "u10_a_l1"
SSW_Z = -1.0  # порог ослабленного вихря (в сигмах)
MIX_COND = 0.55  # доля условного распределения в финальном
MIN_CELLS = 8  # минимум наблюдений в ячейке условной частоты


def ssw_month_flags(u10, lag=None, z=SSW_Z):
    """Флаг месяца: вихрь ослаблен в этом или прошлом месяце (событие
    развивается и держится 4–8 недель, месячное сглаживание неизбежно)."""
    weak = u10 < z
    if lag is not None:
        weak = weak | (lag < z)
    return weak.fillna(False)


class SSWModel(ForecastModel):
    name = "ssw"

    def fit(self, X, y, w=None, edges=None, years=None):
        if U10_COL not in X.columns:
            raise RuntimeError("ssw: нет стратосферного ряда u10")
        self.usable = True
        yv = np.asarray(y, float)
        e1 = np.asarray(edges, float)[:, 0]
        e2 = np.asarray(edges, float)[:, 1]
        lab = np.where(yv < e1, 0, np.where(yv <= e2, 1, 2)).astype(int)
        lag = X[U10_LAG] if U10_LAG in X.columns else None
        self.flag = ssw_month_flags(X[U10_COL], lag).to_numpy(bool)
        self.y = yv
        self.p_cond = []
        self.q_cond = []
        q_all = np.quantile(yv, [0.1, 0.5, 0.9]) if len(yv) else np.zeros(3)
        for f in (True, False):
            m = self.flag == f
            if int(m.sum()) >= MIN_CELLS:
                c = np.bincount(lab[m], minlength=3) / float(m.sum())
                q = np.quantile(yv[m], [0.1, 0.5, 0.9])
            else:
                c = np.full(3, 1.0 / 3.0)
                q = q_all
            c = c + 0.25
            c = c / c.sum()
            self.p_cond.append(0.85 * c + 0.15 * np.full(3, 1.0 / 3.0))
            self.q_cond.append(np.asarray(q, float))
        # безусловное распределение (среднее по ячейкам, взвешенное долей)
        f_mean = float(self.flag.mean())
        self.p_uncond = f_mean * np.asarray(self.p_cond[0]) + (1 - f_mean) * np.asarray(self.p_cond[1])
        return self

    def predict(self, x, e1, e2):
        if not getattr(self, "usable", False):
            raise RuntimeError("ssw: модель не обучена")
        v = float(np.asarray(x[U10_COL], float))
        if U10_LAG in x.index:
            pv = float(np.asarray(x[U10_LAG], float))
            weak = (v < SSW_Z) or (np.isfinite(pv) and pv < SSW_Z)
        else:
            weak = v < SSW_Z
        i = 0 if weak else 1  # p_cond[0] — ячейка «вихрь ослаблен»
        p = MIX_COND * np.asarray(self.p_cond[i]) + (1.0 - MIX_COND) * self.p_uncond
        p = clip_probs(p)
        q = np.asarray(self.q_cond[i], float)
        return p, q

    def analogs(self, x, k=8):
        return []
