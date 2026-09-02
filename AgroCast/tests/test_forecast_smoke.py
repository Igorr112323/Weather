"""Дымовой тест: полный прогноз для точки в Краснодарском крае."""
from pathlib import Path

import pytest

from agrocast.serve.pipeline import world_config

WORLD = Path(__file__).resolve().parents[1] / "world"


@pytest.fixture(scope="module")
def cfg():
    return world_config(WORLD)


def test_forecast_seasonal(cfg):
    import pandas as pd
    from agrocast.forecast.orchestrator import forecast_point

    fc = forecast_point(cfg, 45.03, 39.07, start=pd.Period("2025-12", "M"), horizon=1, variables=("t2m", "tp"), save=False, mode="seasonal", season_len=3)
    assert fc["mode"] == "seasonal"
    seas = fc["seasons"]
    assert len(seas) == 1
    s0 = seas[0]
    for v in ("t2m", "tp"):
        blk = s0[v]
        p = blk["tercile_probs"]
        assert abs(p["below"] + p["normal"] + p["above"] - 1.0) < 0.02
        q = blk["quantiles_c" if v == "t2m" else "quantiles_mm"]
        assert q["p10"] <= q["p50"] <= q["p90"]
        # новые модели в ансамбле (ssw — температурный драйвер, для tp намеренно исключён)
        mp = blk["model_probs"]
        for name in ("deep_analog", "ridge_strat"):
            assert name in mp, f"нет модели {name}: {list(mp)}"
        if v == "t2m":
            assert "ssw" in mp, f"нет ssw для t2m: {list(mp)}"
    # реестр доверия в отчёте (мировые артефакты должны быть переобучены)
    tl = (fc.get("agro") or {}).get("trust_ledger")
    if tl:
        assert "overall" in tl
        assert "p80_coverage" in tl
    assert "what_to_do" in (fc.get("agro") or {}) or fc.get("agro") is None


def test_forecast_monthly(cfg):
    import pandas as pd
    from agrocast.forecast.orchestrator import forecast_point

    fc = forecast_point(cfg, 45.03, 39.07, start=pd.Period("2025-11", "M"), horizon=3, variables=("t2m",), save=False, mode="monthly")
    months = fc["months"]
    assert len(months) == 3
    for m in months:
        p = m["t2m"]["tercile_probs"]
        assert abs(p["below"] + p["normal"] + p["above"] - 1.0) < 0.02
