import json

import pytest

from agrocast.crops.db import CropDB

SEED = [
    {"name": "Гибрид Тестовый 100", "fao": 100, "gdd": 2200, "yield_t_ha": 5.0, "frost_tol_c": -2, "frost_fatal_c": -3, "sow_from": "04-20", "sow_to": "05-15", "notes": "тест"},
]


def make(tmp_path, seed=SEED):
    sp = tmp_path / "seed.json"
    sp.write_text(json.dumps(seed, ensure_ascii=False), encoding="utf-8")
    return CropDB(tmp_path / "c.db", str(sp))


def test_seed_and_all(tmp_path):
    db = make(tmp_path)
    rows = db.all()
    assert len(rows) == 1
    assert rows[0]["name"] == "Гибрид Тестовый 100"
    assert rows[0]["fao"] == 100


def test_persistence(tmp_path):
    a = make(tmp_path)
    a.upsert({"name": "Свой", "fao": 330, "gdd": 2650, "yield_t_ha": 6.2})
    b = CropDB(tmp_path / "c.db", str(tmp_path / "seed.json"))
    r = b.get("Свой")
    assert r and r["gdd"] == 2650
    assert len(b.all()) == 2


def test_upsert_update(tmp_path):
    db = make(tmp_path)
    db.upsert({"name": "Гибрид Тестовый 100", "fao": 100, "gdd": 2200, "yield_t_ha": 7.0})
    r = db.get("Гибрид Тестовый 100")
    assert r["yield_t_ha"] == 7.0
    assert r["gdd"] == 2200
    assert len(db.all()) == 1


def test_remove(tmp_path):
    db = make(tmp_path)
    assert db.remove("Гибрид Тестовый 100") is True
    assert db.get("Гибрид Тестовый 100") is None
    assert db.remove("Гибрид Тестовый 100") is False


def test_validation(tmp_path):
    db = make(tmp_path)
    with pytest.raises(ValueError):
        db.upsert({"name": "   "})
    with pytest.raises(ValueError):
        db.upsert({"name": "X", "gdd": 500})
    with pytest.raises(ValueError):
        db.upsert({"name": "X", "gdd": 5000})
    with pytest.raises(ValueError):
        db.upsert({"name": "X", "yield_t_ha": 99})
    with pytest.raises(ValueError):
        db.upsert({"name": "X", "frost_fatal_c": -1, "frost_tol_c": -5})
    with pytest.raises(ValueError):
        db.upsert({"name": "Гибрид Тестовый 100", "yield_t_ha": -1})
    assert db.get("X") is None
