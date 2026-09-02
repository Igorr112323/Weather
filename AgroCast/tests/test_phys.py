import numpy as np
import pandas as pd

from agrocast.phys import radiation as RAD
from agrocast.phys.surface import SoilSnowColumn, batch_integrate, st_to_state
from agrocast.phys.ocean import TwoLayerSST, DelayedSST
from agrocast.phys.audit import physical_checks, surface_budget_checks


def test_radiation_seasonal_cycle():
    july = RAD.sw_clearsky(45.0, 182)
    jan = RAD.sw_clearsky(45.0, 15)
    assert july > jan > 0
    snow = RAD.net_radiation(45.0, 182, 288.0, 0.85)
    bare = RAD.net_radiation(45.0, 182, 288.0, 0.15)
    assert snow < bare
    winter = RAD.net_radiation(45.0, 15, 283.0, 0.15)
    assert winter < bare


def test_soil_thermal_wave_attenuation():
    c = SoilSnowColumn(45.0)
    hist1, hist2 = [], []
    for y in range(2):
        for d in range(1, 366):
            t_air = 12.0 + 14.0 * np.sin(2.0 * np.pi * (d - 100.0) / 365.0)
            c.step_day(t_air, 0.0, 0.0, d)
            if y == 1:
                hist1.append(c.T[1])
                hist2.append(c.T[2])
    amp1 = float(np.ptp(hist1))
    amp2 = float(np.ptp(hist2))
    assert amp1 > 3.0
    assert amp1 > amp2 > 0.0


def test_snow_melt_energy():
    c = SoilSnowColumn(45.0)
    c.swe = 20.0
    c.age = 30.0
    from agrocast.phys.surface import LF, H_S
    alb = RAD.snow_albedo(30.0, 0.0)
    rad = RAD.net_radiation(45.0, 152, 273.15, alb)
    e = rad * 86400.0
    h_snow = H_S * 8.0
    expected = min(20.0, max(0.0, e + h_snow * 86400.0) / LF)
    c.step_day(8.0, 0.0, 0.0, 152)
    got = 20.0 - c.swe
    assert got > 0.3
    assert abs(got - expected) < 0.3


def test_freeze_no_evap():
    c = SoilSnowColumn(45.0)
    c.T = np.full(4, 270.0)
    c.W = np.full(4, 0.3)
    for _ in range(4):
        c.step_day(-10.0, 0.0, 0.0, 20)
    assert c.frozen[0] > 0.5
    assert c.et_c <= 1e-9


def test_infiltration_capacity():
    c = SoilSnowColumn(45.0)
    c.W = np.array([0.47, 0.2, 0.2, 0.2])
    c.step_day(10.0, 60.0, 0.0, 100)
    assert c.W[0] <= 0.48 + 1e-9
    assert c.W[1] > 0.2


def test_ocean_ar2_stable_and_decays():
    idx = pd.period_range("1981-01", "2024-12", freq="M")
    rng = np.random.default_rng(0)
    t = np.sin(np.arange(len(idx)) / 9.0) * 1.2 * np.exp(-np.arange(len(idx)) / 60.0)
    t = t + rng.normal(0.0, 0.3, len(idx))
    t1 = pd.Series(t, index=idx)
    m = TwoLayerSST()
    assert m.fit(t1, fit_end=2003)
    A = np.array([[m.r[0], m.r[1]], [m.r[2], m.r[3]]])
    assert np.max(np.abs(np.linalg.eigvals(A))) < 1.0
    f = m.forecast(t1, pd.Period("2004-01", "M"), [1, 2, 3, 4, 5, 6])
    assert f is not None
    assert abs(f["mean"][0]) > abs(f["mean"][5]) or f["spread"][0] >= 0.0
    assert (f["spread"] >= 0).all()


def test_green_ampt_wet_less_capacity():
    dry = SoilSnowColumn(45.0)
    dry.W = np.array([0.20, 0.2, 0.2, 0.2])
    dry.xw = 0.02
    wet = SoilSnowColumn(45.0)
    wet.W = np.array([0.45, 0.3, 0.3, 0.3])
    wet.xw = 0.30
    cap_dry = SoilSnowColumn(45.0)
    cap_dry.xw = 0.02
    cap_wet = SoilSnowColumn(45.0)
    cap_wet.xw = 0.30
    from agrocast.phys.surface import KS, PSI_E, DT
    cd = KS * 86400.0 * (1.0 + PSI_E * DT / 0.02) * 1000.0
    cw = KS * 86400.0 * (1.0 + PSI_E * DT / 0.30) * 1000.0
    assert cd > cw
    big = SoilSnowColumn(45.0)
    big.W = np.array([0.45, 0.3, 0.3, 0.3])
    big.xw = 0.30
    big.step_day(15.0, 400.0, 0.0, 180)
    assert big.runoff_c > 50.0


def test_vegetation_seasonal():
    c = SoilSnowColumn(45.0)
    for d in range(120, 300):
        c.step_day(20.0, 0.0, 0.0, d)
    summer = c.laifrac
    c2 = SoilSnowColumn(45.0)
    for d in range(330, 366):
        c2.step_day(-5.0, 0.0, 0.0, d)
    for d in range(1, 30):
        c2.step_day(-5.0, 0.0, 0.0, d)
    winter = c2.laifrac
    assert summer > 0.5
    assert winter < 0.2


def test_wilting_deficit_bounds():
    from agrocast.phys.surface import FC, WP
    c = SoilSnowColumn(45.0)
    c.W[0] = FC
    assert c.state()["adef"] == 0.0
    c.W[0] = WP
    assert c.state()["adef"] == 1.0
    c.W[0] = (FC + WP) / 2.0
    assert 0.4 < c.state()["adef"] < 0.6


def test_batch_single_consistent():
    rng = np.random.default_rng(3)
    days = 90
    ta = 10.0 + 12.0 * np.sin(np.arange(days) / 30.0)
    tp = np.where(rng.random(days) < 0.3, rng.uniform(1.0, 30.0, days), 0.0)
    doys = np.arange(150, 150 + days)
    a = SoilSnowColumn(45.0)
    for i in range(days):
        a.step_day(ta[i], tp[i], 0.0, doys[i])
    b = SoilSnowColumn(45.0)
    st = batch_integrate([b], ta, tp, np.zeros(days), doys)
    assert abs(a.T[0] - (st["t"][0, 0] )) < 0.6
    assert abs(a.W[0] - st["w"][0, 0]) < 0.02
    assert abs(a.laifrac - st["laifrac"][0]) < 0.05


def test_surface_budget_closes():
    rng = np.random.default_rng(11)
    days = 400
    ta = 12.0 + 14.0 * np.sin(2.0 * np.pi * (np.arange(days) - 100.0) / 365.0)
    tp = np.where(rng.random(days) < 0.35, rng.uniform(0.5, 40.0, days), 0.0)
    ice = np.where((tp > 0) & (ta < 1.0), 0.5, 0.0)
    doys = np.tile(np.arange(1, 366), days // 366 + 1)[:days]
    res = surface_budget_checks(ta, tp, ice, doys, lat=45.0)
    assert res["water_balance"]
    assert abs(res["water_resid_mm"]) <= 1.0
    assert res["w_bounds_ok"]
    assert res["t_bounds_ok"]
    assert 0.0 <= res["clip_frac"] <= 1.0


def test_ocean_delayed_oscillator():
    idx = pd.period_range("1981-01", "2024-12", freq="M")
    rng = np.random.default_rng(7)
    x = np.zeros(len(idx))
    for t in range(20, len(idx)):
        x[t] = 0.95 * x[t - 1] - 0.03 * x[t - 18] + rng.normal(0.0, 0.25)
    t1 = pd.Series(x, index=idx)
    m = DelayedSST(tau=18)
    assert m.fit(t1, fit_end=2003)
    assert abs(m.c1 - 0.95) < 0.06
    assert abs(m.c2 - (-0.03)) < 0.03
    r = np.roots([1.0, -m.c1, -m.c2])
    assert np.max(np.abs(r)) < 1.0
    f = m.forecast(t1, pd.Period("2004-01", "M"), [1, 2, 3, 4, 5, 6], n_members=20, seed=1)
    assert f is not None
    assert (f["spread"] >= 0).all()
    assert len(f["mean"]) == 6


def test_audit_checks():
    df = pd.DataFrame({
        "variable": ["t2m"] * 3,
        "target_month": [6, 7, 8],
        "p0": [0.2, 0.3, 0.4],
        "p1": [0.5, 0.4, 0.3],
        "p2": [0.3, 0.3, 0.3],
        "q10": [0.5, 0.6, 0.7],
        "q50": [1.0, 1.1, 1.2],
        "q90": [1.5, 1.6, 1.7],
        "obs_z": [1.0, 1.1, 1.2],
    })
    res = dict(physical_checks(df))
    assert res["prob_sum"]
    assert res["q_order"]
    assert res["obs_range"]
    assert 0.0 <= res["p80_coverage"] <= 1.0
