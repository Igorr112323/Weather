import numpy as np
import pandas as pd

Z90 = 1.2815515655446004


class WeatherGenerator:
    def __init__(self, seed=0):
        self.rng = np.random.default_rng(seed)

    def fit(self, daily):
        df = daily.copy()
        m = df.index.month
        self.tmu = df["t2m"].groupby(m).mean()
        self.tsd = df["t2m"].groupby(m).std().fillna(1.0)
        wet = (df["tp"] >= 1.0).astype(float)
        self.p_wet = wet.groupby(m).mean().clip(0.03, 0.97)
        prev = wet.groupby(m).shift(1)
        self.p_ww = {}
        self.p_dw = {}
        for mm in range(1, 13):
            ok = prev.notna() & (df.index.month == mm)
            wv, pv = wet[ok], prev[ok]
            both = pv == 1.0
            self.p_ww[mm] = float(wv[both].mean()) if both.sum() >= 20 else float(min(self.p_wet[mm] * 1.5, 0.97))
            drym = pv == 0.0
            self.p_dw[mm] = float(wv[drym].mean()) if drym.sum() >= 20 else float(self.p_wet[mm] * 0.7)
        self.gshp = {}
        self.gscl = {}
        allwet = df.loc[df["tp"] >= 1.0, "tp"]
        g_shape, g_scale = self._gamma_moments(allwet.to_numpy())
        for mm in range(1, 13):
            v = df.loc[(df.index.month == mm) & (df["tp"] >= 1.0), "tp"].to_numpy()
            self.gshp[mm], self.gscl[mm] = self._gamma_moments(v) if len(v) >= 10 else (g_shape, g_scale)
        self.amp = (self.tsd * 1.1).clip(1.5, 7.0)
        t_anom = df["t2m"] - self.tmu.reindex(m).to_numpy()
        a1, a2 = t_anom.to_numpy()[:-1], t_anom.to_numpy()[1:]
        if len(a1) > 30 and a1.std() > 1e-6:
            self.rho = float(np.corrcoef(a1, a2)[0, 1])
        else:
            self.rho = 0.6
        self.rho = float(np.clip(self.rho, 0.0, 0.9))
        return self

    @staticmethod
    def _gamma_moments(v):
        v = np.asarray(v, float)
        mu = max(v.mean(), 0.5)
        var = max(v.var(), 0.25)
        shape = mu * mu / var
        scale = var / mu
        return float(np.clip(shape, 0.2, 5.0)), float(np.clip(scale, 0.5, 60.0))

    def generate(self, start, horizon, month_targets, n=200):
        periods = list(pd.period_range(start, periods=horizon, freq="M"))
        series = []
        for _ in range(n):
            frames = []
            prev_anom = 0.0
            wet_state = False
            for p in periods:
                mm = p.month
                tt = month_targets.get((p.year, mm))
                if tt is None:
                    continue
                tq, pq = tt["t_q"], tt["tp_q"]
                tmean_m = self.rng.normal(tq[1], max((tq[2] - tq[0]) / (2 * Z90), 0.3))
                total = max(self.rng.normal(pq[1], max((pq[2] - pq[0]) / (2 * Z90), 2.0)), 0.0)
                ndays = p.days_in_month
                wet_mask = np.zeros(ndays, bool)
                pw = min(self.p_ww[mm] if wet_state else self.p_dw[mm], 0.97)
                for d in range(ndays):
                    wet_state = self.rng.random() < pw
                    wet_mask[d] = wet_state
                    pw = min(self.p_ww[mm] if wet_state else self.p_dw[mm], 0.97)
                if wet_mask.sum() == 0 and total > 0.5:
                    wet_mask[self.rng.integers(0, ndays)] = True
                raw = self.rng.gamma(self.gshp[mm], self.gscl[mm], ndays) * wet_mask
                scale = total / raw.sum() if raw.sum() > 1e-9 else 0.0
                pr = raw * scale
                anom = np.zeros(ndays)
                sd = max(float(self.tsd[mm]), 0.5)
                for d in range(ndays):
                    prev_anom = self.rho * prev_anom + np.sqrt(max(1 - self.rho ** 2, 0.05)) * sd * self.rng.standard_normal()
                    anom[d] = prev_anom * 0.7
                t = tmean_m + anom
                t = t + (tmean_m - t.mean())
                dates = pd.date_range(p.to_timestamp(), periods=ndays, freq="D")
                frames.append(
                    pd.DataFrame(
                        {
                            "t2m": t,
                            "tmin": t - float(self.amp[mm]),
                            "tmax": t + 1.15 * float(self.amp[mm]),
                            "tp": pr,
                        },
                        index=dates,
                    )
                )
            if frames:
                series.append(pd.concat(frames))
        return series
