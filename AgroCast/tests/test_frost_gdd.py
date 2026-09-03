import numpy as np
import pandas as pd

from agrocast.agro.frost import frost_block
from agrocast.agro.insight import season_insight
from agrocast.agro.prices import econ_block

CROP = {
    "name": "Тестовый 300",
    "fao": 300,
    "gdd": 900,
    "vp_days": 110,
    "frost_tol_c": -2,
    "frost_fatal_c": -3,
    "sow_from": "04-20",
    "sow_to": "05-15",
    "yield_t_ha": 6.0,
}


def make_members(n=24, start="2027-03-15", end="2027-09-30", late_frost=6, seed=7):
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, end)
    doy = idx.dayofyear.to_numpy()
    base = 11.5 + 12.0 * np.cos(2.0 * np.pi * (doy - 205) / 365.0)
    out = []
    for i in range(n):
        t2m = base + rng.normal(0, 1.2, len(idx))
        tmin = t2m - 4.0
        if i < late_frost:
            mask = (idx.month == 5) & (idx.day >= 15)
            tmin[mask] = -4.0 - rng.uniform(0, 2, int(mask.sum()))
        df = pd.DataFrame(
            {"t2m": t2m, "tmin": tmin, "tmax": t2m + 4.0, "tp": np.abs(rng.normal(3, 3, len(idx)))},
            index=idx,
        )
        out.append(df)
    return out


def make_monthly():
    idx = pd.period_range("2015-01", "2020-12", freq="M")
    m = idx.month.to_numpy()
    t2m = 11.5 + 12.0 * np.cos(2.0 * np.pi * (m - 7.5) / 12)
    tp = 20 + 15 * np.cos(2.0 * np.pi * (m - 7) / 12)
    return pd.DataFrame({"t2m": t2m, "tp": tp}, index=idx)


def test_frost_generic_and_crop():
    members = make_members()
    g = frost_block(members)
    assert g["p_frost_spring"] >= 0.2
    assert g["p_frost_any"] >= g["p_frost_spring"]
    assert g["last_frost"]["median"] and g["last_frost"]["p90"]
    assert 5 in g["p_frost_day_by_month"]
    c = frost_block(members, CROP)["crop"]
    assert c["safe_date"]
    assert c["danger_at_sow_from"] is not None
    assert c["recommended"]
    assert "окна сорта" in c["verdict"] or "морозоопасно" in c["verdict"]


def test_frost_outside_period():
    members = make_members(start="2026-09-01", end="2026-11-30")
    c = frost_block(members, CROP)["crop"]
    assert "вне горизонта" in c["verdict"]
    assert "safe_date" not in c


def test_frost_safe_later_than_window():
    members = make_members(late_frost=24)
    c = frost_block(members, CROP)["crop"]
    assert c["danger_at_sow_from"] > 0.9
    assert c["safe_date"] == "01.06"
    assert "морозоопасно" in c["verdict"]


def test_gdd_prob_in_season():
    members = make_members()
    monthly = make_monthly()
    res = season_insight(members, 45.25, monthly, 0.3, sat_crops=[CROP])
    row = res["sat"]["gdd"]["crops"][0]
    assert row["harvest"]
    assert 0.0 <= row["p_ok"] <= 1.0
    assert row["gdd"]["p10"] <= row["gdd"]["p50"] <= row["gdd"]["p90"]


def test_gdd_unreachable_need():
    members = make_members()
    monthly = make_monthly()
    crop2 = dict(CROP, name="Поздний 500", fao=500, gdd=5000, vp_days=140)
    res = season_insight(members, 45.25, monthly, 0.3, sat_crops=[CROP, crop2])
    rows = {r["name"]: r for r in res["sat"]["gdd"]["crops"]}
    assert rows["Поздний 500"]["p_ok"] == 0.0
    assert rows["Тестовый 300"]["p_ok"] > rows["Поздний 500"]["p_ok"]


def test_gdd_outside_period():
    members = make_members(start="2026-09-01", end="2026-11-30")
    monthly = make_monthly()
    res = season_insight(members, 45.25, monthly, 0.3, sat_crops=[CROP])
    row = res["sat"]["gdd"]["crops"][0]
    assert row["p_ok"] is None
    assert "вне периода" in row["note"]


def test_econ_irr_m3_rub():
    water = {"irrigation_m3_ha": {"p50": 1500, "p10": 2500}}
    b = econ_block({}, drought_p=0.5, heat_p=0.1, price={"rub_per_t": 18000, "as_of": "2026-09-03", "source": "t", "stale": False}, water=water)
    r = b["crops"][0]
    assert r["irr_m3_ha"] == 1500
    assert r["irr_cost_rub"] == 4500
    assert "3 ₽/м³" in b["source"]
    b2 = econ_block({}, drought_p=0.1, heat_p=0.1)
    assert b2["crops"][0]["irr_m3_ha"] is None
