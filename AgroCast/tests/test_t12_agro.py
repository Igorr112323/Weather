import json
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from agrocast.agro import policy, units  # noqa: E402
from agrocast.agro.advisor import drought_block  # noqa: E402
from agrocast.agro.decide import decision_table  # noqa: E402
from agrocast.agro.insight import season_insight  # noqa: E402
from agrocast.agro.prices import econ_block  # noqa: E402

from test_frost_gdd import CROP, make_members, make_monthly  # noqa: E402


def test_unit_conversion_table():
    assert units.MM_TO_M3_PER_HA == 10.0
    assert units.mm_to_m3_ha(0) == 0
    assert units.mm_to_m3_ha(100) == 1000
    assert units.mm_to_m3_ha(150) == 1500
    assert units.mm_to_m3_ha(12.5) == 125
    assert units.mm_to_m3_ha(-40) == 0
    assert units.mm_to_m3_ha(None) is None
    assert units.mm_to_m3_ha(float("nan")) is None
    assert units.mm_to_m3_ha(15.04, integer=False) == pytest.approx(150.4)
    assert units.m3_ha_to_mm(1500) == pytest.approx(150.0)
    assert units.m3_ha_to_mm(None) is None
    assert units.m3_ha_to_mm(units.mm_to_m3_ha(37.0)) == pytest.approx(37.0)


def test_advisor_hint_units():
    blk = drought_block({"below": 0.7}, {"p50": 40.0}, 120.0)
    assert blk["deficit_mm"] == pytest.approx(80.0)
    assert blk["irrigation_hint_m3_ha"] == 800
    small = drought_block({"below": 0.7}, {"p50": 105.0}, 120.0)
    assert "irrigation_hint_m3_ha" not in small


def _dry_members():
    members = make_members()
    for df in members:
        df["tp"] = np.full(len(df), 0.3)
    return members


def test_water_no_profile_reserve_and_scenario_units():
    res = season_insight(_dry_members(), 45.25, make_monthly(), 0.30, sat_crops=[CROP])
    w = res["water"]
    assert w["reserve_mm"] is None
    assert "не переносится" in w["reserve_note"] or "не рассчитывается" in w["reserve_note"]
    assert w["surface_theta"] == pytest.approx(0.30)
    deficit = w["deficit_mm"]
    assert abs(w["irrigation_m3_ha"]["p50"] - units.mm_to_m3_ha(max(0.0, deficit))) <= 5
    p10 = w["irrigation_m3_ha"]["p10"]
    p50 = w["irrigation_m3_ha"]["p50"]
    assert p10 >= p50 > 0, "сухой сценарий обязан быть не меньше сезонной медианы"
    assert "сценарий" in w["scenario_note"]
    old_bug = round(max(0.0, p10 / 10 * 10 - 0.4 * (0.30 - 0.10) * 2000))
    assert p10 != old_bug or p10 >= 1000


def _cfg_with_skill(tmp_path, skill_obj):
    world = tmp_path / "world"
    (world / "artifacts").mkdir(parents=True, exist_ok=True)
    if skill_obj is not None:
        (world / "artifacts" / "krai_grid_skill.json").write_text(json.dumps(skill_obj))
    cfg = SimpleNamespace(bundle_dir=str(world), artifact_dir=str(tmp_path / "state"), region=SimpleNamespace())
    return cfg


def test_policy_evidence_status_matrix(tmp_path):
    v2_promoted = {"schema": "grid-skill-v2", "combos": {"seasonal_tp_l1": {"skill_promoted": True}, "seasonal_t2m_l1": {"skill_promoted": False}}}
    cfg = _cfg_with_skill(tmp_path / "a", v2_promoted)
    ev = policy.load_evidence(cfg, mode="seasonal")
    assert ev["tp"]["status"] == policy.CONFIRMED
    assert ev["t2m"]["status"] == policy.UNCONFIRMED

    cfg = _cfg_with_skill(tmp_path / "b", {"schema": "legacy", "seasonal_t2m": {"rpss": 0.1}})
    ev = policy.load_evidence(cfg, mode="seasonal")
    assert ev["t2m"]["status"] == policy.UNCONFIRMED

    cfg = _cfg_with_skill(tmp_path / "c", None)
    ev = policy.load_evidence(cfg, mode="seasonal")
    assert all(d["status"] == policy.UNAVAILABLE for d in ev.values())

    cfg = _cfg_with_skill(tmp_path / "d", "broken")
    (cfg.bundle_dir + "").__str__
    path = Path(cfg.bundle_dir) / "artifacts" / "krai_grid_skill.json"
    path.write_text("{not json")
    ev = policy.load_evidence(cfg, mode="seasonal")
    assert all(d["status"] == policy.UNAVAILABLE for d in ev.values())
    assert "unreadable" in ev["t2m"]["source"]


def test_tag_agro_and_directives(tmp_path):
    cfg = _cfg_with_skill(tmp_path, {"schema": "grid-skill-v2",
                                     "combos": {"seasonal_tp_l1": {"skill_promoted": False}, "seasonal_t2m_l1": {"skill_promoted": False}}})
    ev = policy.load_evidence(cfg, mode="seasonal")
    agro = {
        "insight": {
            "water": {"irrigation_m3_ha": {"p50": 300}},
            "drought": {"irrigation_hint_m3_ha": 500, "risk_level": "высокий"},
            "frost": {"crop": {"safe_date": "05.05"}},
        },
        "decisions": [{"key": "irrigate", "label": "Полив", "prob": 0.5, "ratio": 0.3, "verdict": "решить после проверки состояния поля: вероятность ориентировочная (навык прогноза tp не подтверждён)", "evidence_variable": "tp"}],
        "econ": {"crops": [{"key": "maize", "risk_rub_ha": 9000, "area_note": ""}]},
        "phenology": {"crops": [{"name": "Кукуруза", "stages": [{"name": "Налив", "crit": "drought", "prob": 0.6, "action": "полив по декадному балансу"}]}]},
    }
    policy.tag_agro(agro, ev)
    assert agro["insight"]["drought"]["policy"]["status"] == policy.UNCONFIRMED
    from agrocast.forecast.orchestrator import _what_to_do

    acts = _what_to_do(agro)
    text = json.dumps(acts, ensure_ascii=False)
    assert "не предписание" in text
    assert all(c["level"] != "high" for c in acts)
    assert any("полив" in c["action"].lower() for c in acts)

    cfg2 = _cfg_with_skill(tmp_path / "u", None)
    ev2 = policy.load_evidence(cfg2, mode="seasonal")
    agro2 = json.loads(json.dumps(agro))
    agro2["policy"] = None
    policy.tag_agro(agro2, ev2)
    assert "irrigation_hint_m3_ha" not in agro2["insight"]["drought"]
    acts2 = _what_to_do(agro2)
    assert all("полив" not in c["action"].lower() or "Ориентировочно" in c["action"] for c in acts2)
    assert not any("Запланировать полив" in c["action"] for c in acts2)


def test_decision_table_policy_variants():
    ev_ok = {"tp": {"status": policy.CONFIRMED}, "t2m": {"status": policy.CONFIRMED}}
    rows = decision_table(0.7, 0.1, evidence=ev_ok)
    assert rows[0]["verdict"].startswith("действовать")
    ev_bad = {"tp": {"status": policy.UNCONFIRMED}, "t2m": {"status": policy.CONFIRMED}}
    rows = decision_table(0.7, 0.1, evidence=ev_bad)
    assert rows[0]["verdict"].startswith("решить после проверки")
    assert "не подтверждён" in rows[0]["verdict"]
    rows = decision_table(0.7, 0.1, evidence={"tp": {"status": policy.UNAVAILABLE}, "t2m": {"status": policy.UNAVAILABLE}})
    assert not any(r["verdict"].startswith("действовать") for r in rows)
    assert rows[0]["verdict"].startswith("без предписания")
    rows = decision_table(0.05, 0.05, evidence=ev_ok)
    assert all(r["verdict"] in ("не окупается", "на грани — решать вам") for r in rows)
    assert not any(r["verdict"].startswith("действовать") for r in rows)


def test_econ_money_is_scenario_estimate():
    price = {"rub_per_t": 18000, "as_of": "2026-09", "source": "тест", "stale": False}
    b = econ_block({}, drought_p=0.8, heat_p=0.1, price=price, water={"irrigation_m3_ha": {"p50": 1500, "p10": 2000}})
    r = b["crops"][0]
    assert "не гарантия дохода" in b["source"]
    assert "не гарантия дохода" in r["money_note"]
    assert "сценарн" in r["irrigation"] or "окупается" in r["irrigation"]
    assert r["irr_m3_ha"] == 1500
    assert r["irr_cost_rub"] == 4500
    b2 = econ_block({}, drought_p=0.05, heat_p=0.05, price=price)
    assert b2["crops"][0]["irr_m3_ha"] is None


def test_digest_lines_honest_labeling():
    from agrocast.serve.digest import field_letter

    payload = {
        "seasons": [],
        "agro": {
            "insight": {
                "water": {
                    "policy": {"status": policy.UNCONFIRMED},
                    "surface_theta": 0.31,
                    "precip_mm": {"p50": 60},
                    "deficit_mm": 40,
                    "reserve_mm": None,
                    "irrigation_m3_ha": {"p50": 400, "p10": 520},
                },
            },
            "econ": {"crops": [{"key": "maize", "risk_rub_ha": 5000, "cost_irr_rub": 4500, "cost_anti_rub": 1100,
                                 "irrigation": "окупается (по сценарной оценке)", "antistress": "не окупается"}]},
        },
    }
    txt = field_letter({"name": "Северное", "crop": "maize", "area": 12.0, "lat": 45.1, "lon": 39.2}, payload)
    assert "не предписание" in txt
    assert "0–7 см" in txt
    assert "запас корнеобитаемого слоя не верифицирован" in txt
    assert "сценарная оценка, не гарантия дохода" in txt
    assert "сухой сценарий (ET0 p90 / осадки p10)" in txt

    payload_un = json.loads(json.dumps(payload))
    payload_un["agro"]["insight"]["water"]["policy"]["status"] = policy.UNAVAILABLE
    txt2 = field_letter({"name": "N", "crop": "maize", "area": 1.0, "lat": 1, "lon": 1}, payload_un)
    assert "Полив" not in txt2


def test_field_point_binding_in_orchestrator_payload():
    from agrocast.serve.pipeline import world_config
    from agrocast.forecast.orchestrator import forecast_point

    WORLD = ROOT / "world"
    if not (WORLD / "zarr").exists():
        pytest.skip("нет bundle world")
    cfg = world_config(WORLD, Path("/tmp/agrocast-pgtest/t12-state"))
    lat, lon = 45.0311, 39.0722
    fc = forecast_point(cfg, lat, lon, start=pd.Period("2025-12", "M"), horizon=1, variables=("t2m", "tp"), save=False, mode="seasonal")
    assert fc["lat"] == pytest.approx(lat, abs=1e-9)
    assert fc["lon"] == pytest.approx(lon, abs=1e-9)
    gp = fc["grid_point"]
    assert abs(gp["lat"] - lat) <= 0.5 and abs(gp["lon"] - lon) <= 0.5
    assert "policy" in (fc.get("agro") or {}) or (fc.get("agro") or {}).get("what_to_do") is not None
