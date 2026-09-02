import numpy as np
import pandas as pd

from agrocast.phys import radiation as RAD

KV = 1.8
RCVOL = 2.4e6
LV = 2.5e6
LF = 2.83e5
H_S = 2.0
ALB_BARE = 0.15
FREEZE_SPAN = 10.0

T_BOT = 281.0
W_INIT = 0.28
CAP = 0.48
FC = 0.35
DRAIN = 0.005
WP = 0.12
DT = CAP - WP
KS = 1.0e-6
PSI_E = 0.30
LAI_MAX = 3.0
GDD_SCALE = 300.0
GDD_BASE = 5.0
DORM_BASE = -5.0


class SoilSnowColumn:
    def __init__(self, lat):
        self.lat = lat
        self.dz = np.array([0.10, 0.30, 0.50, 1.10])
        self.T = np.full(4, T_BOT + 2.0)
        self.W = np.full(4, W_INIT)
        self.frozen = np.zeros(4)
        self.swe = 0.0
        self.age = 0.0
        self.et_c = 0.0
        self.fz_count = 0
        self.melt_idx = np.nan
        self._had_snow = False
        self._was_frozen = False
        self.thflux = 0.0
        self.xw = 0.05
        self.gdd = 0.0
        self.laifrac = 0.0
        self.runoff_c = 0.0
        self.drain_out = 0.0
        self.clip_count = 0
        self.energy_resid_max = 0.0
        self.cap_loss = 0.0

    def clone(self):
        c = SoilSnowColumn(self.lat)
        c.T = self.T.copy()
        c.W = self.W.copy()
        c.frozen = self.frozen.copy()
        c.swe = self.swe
        c.age = self.age
        c.et_c = self.et_c
        c.fz_count = self.fz_count
        c.melt_idx = self.melt_idx
        c._had_snow = self._had_snow
        c._was_frozen = self._was_frozen
        c.thflux = self.thflux
        c.xw = self.xw
        c.gdd = self.gdd
        c.laifrac = self.laifrac
        c.runoff_c = self.runoff_c
        c.drain_out = self.drain_out
        c.clip_count = self.clip_count
        c.energy_resid_max = self.energy_resid_max
        c.cap_loss = self.cap_loss
        return c

    def state(self):
        s = {}
        for i in range(4):
            s[f"t{i+1}"] = self.T[i] - 273.15
            s[f"w{i+1}"] = self.W[i]
        s["swe"] = self.swe
        s["snowcov"] = 1.0 if self.swe > 1.0 else 0.0
        s["frozen_cm"] = self.frozen[0] * 10.0
        s["thflux"] = self.thflux
        s["et_c"] = self.et_c
        s["fztw"] = float(self.fz_count)
        s["swdef"] = max(0.0, self.W[0] - WP)
        s["adef"] = float(np.clip(1.0 - (self.W[0] - WP) / (FC - WP), 0.0, 1.0))
        s["laifrac"] = float(self.laifrac)
        s["runoff_c"] = float(self.runoff_c)
        s["melt_idx"] = self.melt_idx
        return s

    def assimilate_swvl(self, obs):
        if obs is None or not np.isfinite(obs):
            return
        self.W[0] = 0.5 * self.W[0] + 0.5 * float(np.clip(obs, 0.05, CAP))

    def assimilate_snow(self, cover):
        if cover is None or not np.isfinite(cover):
            return
        if cover > 0.2 and self.swe < 1.0:
            self.swe = max(self.swe, 10.0 * float(cover))
            self._had_snow = True
        elif cover < 0.05 and self.swe < 1.0:
            self.swe = 0.0
            self._had_snow = False
            self.melt_idx = 0.0

    def _vegetate(self, t_air):
        t = float(t_air)
        if t > GDD_BASE:
            self.gdd += t - GDD_BASE
        elif t < DORM_BASE:
            self.gdd = 0.0
        self.laifrac = 1.0 - np.exp(-self.gdd / GDD_SCALE)
        return self.laifrac

    def _green_ampt(self, tp_mm):
        i_cap = KS * 86400.0 * (1.0 + PSI_E * DT / max(self.xw, 0.02)) * 1000.0
        infil = min(float(tp_mm), i_cap)
        self.runoff_c += float(tp_mm) - infil
        self.W[0] += infil / 1000.0 / self.dz[0]
        return infil

    def step_day(self, t_air, tp, ice, doy):
        t_air = float(t_air)
        tp = float(tp)
        ice = float(ice)
        if not np.isfinite(t_air):
            return
        if not np.isfinite(tp):
            tp = 0.0
        w0_prev = self.W[0]
        fresh = 0.0
        if self.swe > 0.5:
            fresh = 1.0 if tp * ice > 0.1 else 0.0
            alb = RAD.snow_albedo(self.age, fresh)
        else:
            vf = self._vegetate(t_air)
            alb = (1.0 - vf) * ALB_BARE + vf * 0.20
        rad = RAD.net_radiation(self.lat, int(doy), 273.15 if self.swe > 0.5 else self.T[0], alb)
        e = rad * 86400.0
        t_snow = self.swe > 0.5
        et_mm = 0.0
        if t_snow:
            tp_ice = tp * ice
            tp_liq = tp - tp_ice
            h_snow = H_S * max(0.0, t_air)
            melt_mm = min(self.swe, max(0.0, e + h_snow * 86400.0) / LF)
            liq_melt = min(self.swe - melt_mm, tp_liq)
            self.swe -= melt_mm + liq_melt
            self.swe += tp_ice
            if tp_ice > 0.1:
                self.age = 0.0
            else:
                self.age += 1.0
            g_flux = KV * (273.15 - self.T[1]) / self.dz[0]
            self.thflux = float(g_flux)
            self.T[0] = 273.15
            if self.swe > 1.0:
                self._had_snow = True
                self.melt_idx = np.nan
            else:
                self._had_snow = False
                if self.melt_idx != np.nan:
                    self.melt_idx = 0.0
        else:
            g_flux = float(np.clip(KV * (self.T[0] - self.T[1]) / self.dz[0], -60.0, 60.0))
            self.thflux = g_flux
            e_soil = e * 0.25
            h_s = H_S * (t_air - (self.T[0] - 273.15))
            et_e = max(0.0, e_soil + h_s * 86400.0 - g_flux * 86400.0) * 0.7
            et_e *= 0.25 + 0.75 * vf
            et_mm = et_e / LV
            avail_mm = max(0.0, self.W[0] - self.frozen[0] * (self.W[0] - WP) - WP) * self.dz[0] * 1000.0
            et_mm = min(et_mm, avail_mm)
            if self.T[0] < 273.15:
                et_mm = 0.0
            self.W[0] -= et_mm / 1000.0 / self.dz[0]
            self.et_c += et_mm
            if tp > 0.0:
                self._green_ampt(tp)
            d_t0 = (e_soil + h_s * 86400.0 - g_flux * 86400.0 - LV * et_mm) / (RCVOL * self.dz[0] / 2.0)
            if d_t0 > 3.0 or d_t0 < -3.0:
                self.clip_count += 1
                resid = (d_t0 - float(np.clip(d_t0, -3.0, 3.0))) * (RCVOL * self.dz[0] / 2.0)
                self.energy_resid_max = max(self.energy_resid_max, abs(resid))
            self.T[0] += float(np.clip(d_t0, -3.0, 3.0))
        if self.swe <= 0.5 and not t_snow and self._had_snow:
            if self.melt_idx == np.nan:
                self.melt_idx = 0.0
        elif self._had_snow and self.melt_idx == 0.0:
            pass
        if self.melt_idx == 0.0 and self._had_snow:
            self.melt_idx = np.nan
        if not t_snow and self.swe <= 0.5 and not self._had_snow:
            self.melt_idx = np.nan
        self._cn_diffuse()
        if self.T[0] < 273.15:
            f = min(1.0, max(0.0, (273.15 - self.T[0]) / FREEZE_SPAN))
            self.frozen[0] = max(self.frozen[0], f)
            if not self._was_frozen:
                self.fz_count += 1
                self._was_frozen = True
        elif self.T[0] >= 273.15 + 2.0:
            self.frozen[0] = 0.0
            self._was_frozen = False
        if self.W[0] > CAP:
            over = (self.W[0] - CAP) * self.dz[0]
            self.W[0] = CAP
            for i in range(1, 4):
                need = over
                room = (CAP - self.W[i])
                if room > 0:
                    take = min(over, room * self.dz[i])
                    self.W[i] += take / self.dz[i]
                    over -= take
                if over <= 0:
                    break
            if over > 0:
                self.cap_loss += over * 1000.0
        for i in range(4):
            liquid = self.W[i] - self.frozen[i] * (self.W[i] - WP)
            if liquid > FC:
                excess = min(liquid - FC, DRAIN / self.dz[i])
                self.W[i] -= excess
                if i < 3:
                    add = excess * self.dz[i] / self.dz[i + 1]
                    new_w = self.W[i + 1] + add
                    if new_w > CAP:
                        self.cap_loss += (new_w - CAP) * self.dz[i + 1] * 1000.0
                        new_w = CAP
                    self.W[i + 1] = new_w
                else:
                    self.drain_out += excess * self.dz[i] * 1000.0
        d_w0 = (self.W[0] - w0_prev) * self.dz[0] * 1000.0
        self.xw = float(np.clip(self.xw + d_w0 / 1000.0 / DT, 0.02, 2.0))

    def _cn_diffuse(self):
        k = KV / RCVOL
        dt = 86400.0
        dz0, dz1, dz2, dz3 = self.dz
        d12 = (dz0 + dz1) / 2.0
        d23 = (dz1 + dz2) / 2.0
        d34 = (dz2 + dz3) / 2.0
        a1 = k * dt / (dz1 * d12)
        b1 = k * dt / (dz1 * d23)
        a2 = k * dt / (dz2 * d23)
        b2 = k * dt / (dz2 * d34)
        a3 = k * dt / (dz3 * d34)
        b3 = k * dt / (dz3 * dz3)
        t0 = self.T[0]
        t1, t2, t3 = self.T[1], self.T[2], self.T[3]
        A = np.array([
            [1.0 + (a1 + b1) / 2.0, -b1 / 2.0, 0.0],
            [-a2 / 2.0, 1.0 + (a2 + b2) / 2.0, -b2 / 2.0],
            [0.0, -a3 / 2.0, 1.0 + (a3 + b3) / 2.0],
        ])
        B = np.array([
            (1.0 - (a1 + b1) / 2.0) * t1 + a1 * t0 + (b1 / 2.0) * t2,
            (1.0 - (a2 + b2) / 2.0) * t2 + (a2 / 2.0) * t1 + (b2 / 2.0) * t3,
            (1.0 - (a3 + b3) / 2.0) * t3 + (a3 / 2.0) * t2 + b3 * T_BOT,
        ])
        sol = np.linalg.solve(A, B)
        self.T[1], self.T[2], self.T[3] = sol

def batch_integrate(cols, t_air, tp, ice, doys):
    n = len(cols)
    d = len(t_air)
    st = {
        "t": np.stack([c.T for c in cols]),
        "w": np.stack([c.W for c in cols]),
        "frozen0": np.array([c.frozen[0] for c in cols]),
        "swe": np.array([c.swe for c in cols]),
        "age": np.array([c.age for c in cols]),
        "et_c": np.array([c.et_c for c in cols]),
        "fz": np.array([c.fz_count for c in cols], float),
        "had_snow": np.array([c._had_snow for c in cols]),
        "was_frozen": np.array([c._was_frozen for c in cols]),
        "melt_idx": np.array([c.melt_idx for c in cols], float),
        "thflux": np.array([c.thflux for c in cols]),
        "xw": np.array([c.xw for c in cols]),
        "gdd": np.array([c.gdd for c in cols]),
        "laifrac": np.array([c.laifrac for c in cols]),
        "runoff": np.array([c.runoff_c for c in cols], float),
        "caploss": np.array([c.cap_loss for c in cols], float),
    }
    tbot = T_BOT
    for i in range(d):
        ta = t_air[i]
        tp_ = tp[i]
        ice_ = ice[i]
        doy = int(doys[i])
        t_snow_v = st["swe"] > 0.5
        bare_v = ~t_snow_v
        st["gdd"] = np.where(bare_v & (ta > GDD_BASE), st["gdd"] + (ta - GDD_BASE), st["gdd"])
        st["gdd"] = np.where(bare_v & (ta < DORM_BASE), 0.0, st["gdd"])
        st["laifrac"] = np.where(bare_v, 1.0 - np.exp(-st["gdd"] / GDD_SCALE), st["laifrac"])
        fresh = np.where((st["swe"] > 0.5) & (tp_ * ice_ > 0.1), 1.0, 0.0)
        alb = np.where(st["swe"] > 0.5, np.maximum(0.40, 0.85 - 0.012 * st["age"]), ALB_BARE)
        alb = np.where((st["swe"] > 0.5) & (fresh > 0.5), 0.85, alb)
        alb = np.where(bare_v, (1.0 - st["laifrac"]) * ALB_BARE + st["laifrac"] * 0.20, alb)
        rad = RAD.net_radiation(cols[0].lat, doy, np.where(st["swe"] > 0.5, 273.15, st["t"][:, 0]), alb)
        e = rad * 86400.0
        t_snow = st["swe"] > 0.5
        w0_prev = st["w"][:, 0].copy()
        h_snow = H_S * np.maximum(0.0, ta)
        melt_mm = np.where(t_snow, np.minimum(st["swe"], np.maximum(0.0, e + h_snow * 86400.0) / LF), 0.0)
        tp_ice = tp_ * ice_
        tp_liq = tp_ - tp_ice
        liq_melt = np.where(t_snow, np.minimum(np.maximum(st["swe"] - melt_mm, 0.0), tp_liq), 0.0)
        st["swe"] = st["swe"] - melt_mm - liq_melt + np.where(t_snow, tp_ice, 0.0)
        st["age"] = np.where((t_snow) & (tp_ice > 0.1), 0.0, st["age"] + 1.0)
        g_flux = np.where(t_snow, KV * (273.15 - st["t"][:, 1]) / cols[0].dz[0],
                          KV * (st["t"][:, 0] - st["t"][:, 1]) / cols[0].dz[0])
        g_flux = np.clip(g_flux, -60.0, 60.0)
        st["thflux"] = g_flux
        h_s = np.where(t_snow, 0.0, H_S * (ta - (st["t"][:, 0] - 273.15)))
        avail_mm = np.maximum(0.0, st["w"][:, 0] - st["frozen0"] * (st["w"][:, 0] - WP) - WP) * cols[0].dz[0] * 1000.0
        et_e = np.where(t_snow, 0.0, np.maximum(0.0, e * 0.25 + h_s * 86400.0 - g_flux * 86400.0) * 0.7)
        et_e = et_e * np.where(t_snow, 1.0, 0.25 + 0.75 * st["laifrac"])
        et_mm_ = et_e / LV
        et_mm_ = np.minimum(et_mm_, avail_mm)
        et_mm_ = np.where(st["t"][:, 0] < 273.15, 0.0, et_mm_)
        st["et_c"] = st["et_c"] + et_mm_
        st["w"][:, 0] = st["w"][:, 0] - et_mm_ / 1000.0 / cols[0].dz[0]
        st["t"][:, 0] = np.where(t_snow, 273.15, st["t"][:, 0])
        bare = ~t_snow
        d_t0 = np.where(bare, (e * 0.25 + h_s * 86400.0 - g_flux * 86400.0 - LV * et_mm_) / (RCVOL * cols[0].dz[0] / 2.0), 0.0)
        st["t"][:, 0] = st["t"][:, 0] + np.where(bare, np.clip(d_t0, -3.0, 3.0), 0.0)
        i_cap = KS * 86400.0 * (1.0 + PSI_E * DT / np.maximum(st["xw"], 0.02)) * 1000.0
        tp_bare = np.where(bare, np.maximum(0.0, tp_), 0.0)
        infil = np.minimum(tp_bare, i_cap)
        st["runoff"] = st["runoff"] + (tp_bare - infil)
        st["w"][:, 0] = st["w"][:, 0] + infil / 1000.0 / cols[0].dz[0]
        st["melt_idx"] = np.where((st["swe"] > 1.0) & st["had_snow"], np.nan, st["melt_idx"])
        st["melt_idx"] = np.where((st["swe"] <= 1.0) & (st["melt_idx"] != np.nan), 0.0, st["melt_idx"])
        had_prev = st["had_snow"]
        st["had_snow"] = st["swe"] > 1.0
        st["melt_idx"] = np.where((st["swe"] <= 1.0) & had_prev & (st["melt_idx"] == 0.0), np.nan, st["melt_idx"])
        st["had_snow"] = (st["swe"] > 1.0) | (st["melt_idx"] == 0.0)
        t_prev = st["t"][:, 0].copy()
        sub = {"t": st["t"].copy()}
        _cn_batch(sub, tbot)
        st["t"] = sub["t"]
        st["t"][:, 0] = np.where(t_snow, 273.15, st["t"][:, 0])
        f = np.clip((273.15 - st["t"][:, 0]) / FREEZE_SPAN, 0.0, 1.0)
        st["frozen0"] = np.maximum(st["frozen0"], f)
        newfz = (st["t"][:, 0] < 273.15) & (t_prev >= 273.15) & ~st["was_frozen"]
        st["fz"] = st["fz"] + newfz.astype(float)
        st["was_frozen"] = np.where(st["t"][:, 0] < 273.15, True, st["was_frozen"])
        st["was_frozen"] = np.where(st["t"][:, 0] >= 273.15 + 2.0, False, st["was_frozen"])
        st["frozen0"] = np.where(st["t"][:, 0] >= 273.15 + 2.0, 0.0, st["frozen0"])
        over = np.maximum(0.0, st["w"][:, 0] - CAP) * cols[0].dz[0]
        st["w"][:, 0] = np.minimum(st["w"][:, 0], CAP)
        for i2 in range(1, 4):
            room = (CAP - st["w"][:, i2]) * cols[0].dz[i2]
            take = np.minimum(over, np.maximum(0.0, room))
            st["w"][:, i2] = st["w"][:, i2] + take / cols[0].dz[i2]
            over = over - take
        for i2 in range(4):
            liquid = st["w"][:, i2] - st["frozen0"] * (st["w"][:, i2] - WP) if i2 == 0 else st["w"][:, i2]
            excess = np.minimum(np.maximum(0.0, liquid - FC), DRAIN / cols[0].dz[i2])
            st["w"][:, i2] = st["w"][:, i2] - excess
            if i2 < 3:
                add = excess * cols[0].dz[i2] / cols[0].dz[i2 + 1]
                new_w = st["w"][:, i2 + 1] + add
                st["caploss"] = st["caploss"] + np.maximum(0.0, new_w - CAP) * cols[0].dz[i2 + 1] * 1000.0
                st["w"][:, i2 + 1] = np.minimum(CAP, new_w)
        d_w0 = (st["w"][:, 0] - w0_prev) * cols[0].dz[0] * 1000.0
        st["xw"] = np.clip(st["xw"] + d_w0 / 1000.0 / DT, 0.02, 2.0)
    return st


def _cn_batch(st, tbot):
    dz0, dz1, dz2, dz3 = 0.10, 0.30, 0.50, 1.10
    k = KV / RCVOL
    dt = 86400.0
    d12 = (dz0 + dz1) / 2.0
    d23 = (dz1 + dz2) / 2.0
    d34 = (dz2 + dz3) / 2.0
    a1 = k * dt / (dz1 * d12)
    b1 = k * dt / (dz1 * d23)
    a2 = k * dt / (dz2 * d23)
    b2 = k * dt / (dz2 * d34)
    a3 = k * dt / (dz3 * d34)
    b3 = k * dt / (dz3 * dz3)
    t0 = st["t"][:, 0]
    t1 = st["t"][:, 1]
    t2 = st["t"][:, 2]
    t3 = st["t"][:, 3]
    A = np.array([
        [1.0 + (a1 + b1) / 2.0, -b1 / 2.0, 0.0],
        [-a2 / 2.0, 1.0 + (a2 + b2) / 2.0, -b2 / 2.0],
        [0.0, -a3 / 2.0, 1.0 + (a3 + b3) / 2.0],
    ])
    B = np.stack([
        (1.0 - (a1 + b1) / 2.0) * t1 + a1 * t0 + (b1 / 2.0) * t2,
        (1.0 - (a2 + b2) / 2.0) * t2 + (a2 / 2.0) * t1 + (b2 / 2.0) * t3,
        (1.0 - (a3 + b3) / 2.0) * t3 + (a3 / 2.0) * t2 + b3 * tbot,
    ], axis=0)
    sol = np.linalg.solve(A, B)
    st["t"][:, 1], st["t"][:, 2], st["t"][:, 3] = sol[0], sol[1], sol[2]
    return t0


def st_to_state(st, i):
    return {
        "t1": float(st["t"][i, 0] - 273.15),
        "t2": float(st["t"][i, 1] - 273.15),
        "t3": float(st["t"][i, 2] - 273.15),
        "t4": float(st["t"][i, 3] - 273.15),
        "w1": float(st["w"][i, 0]),
        "w2": float(st["w"][i, 1]),
        "w3": float(st["w"][i, 2]),
        "w4": float(st["w"][i, 3]),
        "swe": float(st["swe"][i]),
        "snowcov": 1.0 if st["swe"][i] > 1.0 else 0.0,
        "frozen_cm": float(st["frozen0"][i] * 10.0),
        "thflux": float(st["thflux"][i]),
        "et_c": float(st["et_c"][i]),
        "fztw": float(st["fz"][i]),
        "swdef": float(max(0.0, st["w"][i, 0] - WP)),
        "adef": float(np.clip(1.0 - (st["w"][i, 0] - WP) / (FC - WP), 0.0, 1.0)),
        "laifrac": float(st["laifrac"][i]),
        "runoff_c": float(st["runoff"][i]),
        "melt_idx": float(st["melt_idx"][i]) if np.isfinite(st["melt_idx"][i]) else np.nan,
    }
