import json
from datetime import datetime, timezone
from pathlib import Path

from agrocast.agro import corn as corn_params
from agrocast.agro.prices import econ_block
from agrocast.market import source as mkt

BASE = Path(__file__).resolve().parent.parent
TODAY = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")


def test_corn_params_sourced():
    c = corn_params.CORN
    assert c["key"] == "maize"
    gdd = [g["gdd"] for g in c["maturity_groups"]]
    assert gdd == sorted(gdd) and gdd[0] == 2200 and len(gdd) == 4
    f = c["frost"]
    assert f["seedlings_fatal_c"] < f["seedlings_ok_to_c"] < 0
    assert c["gdd_base_c"] == 10.0
    assert c["sow_window"]["months"] == [4, 5]
    assert c["gdd_source"] and f["source"] and c["sow_window"]["source"]
    assert 5.0 <= c["yield_t_ha"] <= 8.0


def test_econ_block_corn_only_with_market_price():
    ph = {"crops": [{"key": "maize", "stages": [{"crit": "засуха", "prob": 0.5}, {"crit": "жара >33°C", "prob": 0.2}]}]}
    price = {
        "rub_per_t": 18000,
        "as_of": "2026-09-03",
        "source": "CBOT (Chicago Board of Trade)",
        "stale": False,
        "usd_per_t": 208.0,
        "stale_days": 0,
    }
    out = econ_block(ph, None, None, price=price)
    assert len(out["crops"]) == 1
    row = out["crops"][0]
    assert row["key"] == "maize"
    assert row["price_rub_t"] == 18000
    assert row["drought_p"] == 0.5
    assert row["heat_p"] == 0.2
    assert row["loss_drought_rub"] > 0
    assert "CBOT" in out["source"]
    assert "2026-09-03" in out["source"]


def test_corn_price_fallback_to_seed(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise RuntimeError("нет сети")

    monkeypatch.setattr(mkt, "fetch_corn_price", boom)
    p = mkt.corn_price(str(tmp_path), str(BASE / "world"), timeout=1)
    assert p["rub_per_t"] > 5000
    assert p["as_of"]
    assert p.get("note")


def test_corn_price_cache_fresh_skips_fetch(monkeypatch, tmp_path):
    called = {"n": 0}

    def boom(*a, **k):
        called["n"] += 1
        raise RuntimeError("нет сети")

    monkeypatch.setattr(mkt, "fetch_corn_price", boom)
    cache_dir = tmp_path / "market"
    cache_dir.mkdir()
    (cache_dir / "corn_price.json").write_text(
        json.dumps({"rub_per_t": 17000, "as_of": TODAY, "source": "cache", "fetched_at": "2026-09-03T01:00:00+00:00"})
    )
    p = mkt.corn_price(str(tmp_path), str(BASE / "world"), timeout=1)
    assert called["n"] == 0
    assert p["rub_per_t"] == 17000
