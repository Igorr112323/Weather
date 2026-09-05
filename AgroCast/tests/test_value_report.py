import json

import numpy as np
import pandas as pd
import pytest

from scripts.value_report import (build_report, decomposition, render_md, segment_stats,
                                  write_outputs, years_block)
from agrocast.serve.product import value_api, value_page


def _rps_arr(probs, obs):
    probs = np.asarray(probs, float)
    obs = np.asarray(obs, int)
    c = np.cumsum(probs, axis=1)
    o = np.zeros_like(c)
    o[np.arange(len(obs)), obs] = 1.0
    o = np.cumsum(o, axis=1)
    return ((c - o) ** 2).sum(axis=1)


def _mk_df(obs, p, bp=None, year=None, mode="seasonal", variable="t2m"):
    obs = np.asarray(obs, int)
    p = np.asarray(p, float).reshape(len(obs), 3)
    n = len(obs)
    bp = p if bp is None else np.asarray(bp, float).reshape(n, 3)
    year = np.full(n, 2010) if year is None else np.asarray(year, int)
    return pd.DataFrame({
        "point": [f"P{(i % 28) + 1:02d}" for i in range(n)],
        "mode": [mode] * n,
        "variable": [variable] * n,
        "year": year,
        "season": ["DJF"] * n,
        "p0": p[:, 0], "p1": p[:, 1], "p2": p[:, 2],
        "bp0": bp[:, 0], "bp1": bp[:, 1], "bp2": bp[:, 2],
        "obs_tercile": obs,
        "rps": _rps_arr(p, obs),
        "brps": _rps_arr(bp, obs),
        "rps_c": _rps_arr(np.tile([1 / 3, 1 / 3, 1 / 3], (n, 1)), obs),
        "hit": (p.argmax(1) == obs).astype(int),
        "in_corridor_c": np.ones(n),
    })


def _perfect_balanced():
    obs = np.array([0] * 100 + [1] * 100 + [2] * 100)
    p = np.eye(3)[obs]
    return obs, p


def test_segment_stats_win_rate_rpss_and_calibration():
    obs, p = _perfect_balanced()
    df = _mk_df(obs, p)
    s = segment_stats(df, "seasonal", "t2m")
    assert s["n"] == 300
    assert s["rpss"] == pytest.approx(1.0)
    assert s["hit"] == pytest.approx(1.0)
    assert s["win"] == pytest.approx(1.0)
    assert s["rps_clim"] == pytest.approx(0.4444, abs=1e-5)
    assert s["rps"] == pytest.approx(0.0)
    assert s["ece"] == pytest.approx(0.6667, abs=1e-4)
    for k in ("below", "normal", "above"):
        assert s["freq"][k] == pytest.approx(0.3333, abs=1e-4)
        assert s["claimed"][k] == pytest.approx(0.3333, abs=1e-4)
    assert segment_stats(df, "monthly", "tp") is None


def test_years_block_positive_count_decades_best_worst():
    obs15 = np.array([0] * 5 + [1] * 5 + [2] * 5)
    years = np.repeat(np.arange(2005, 2025), 15)
    p = np.tile(np.eye(3)[obs15], (20, 1))
    obs_all = np.tile(obs15, 20)
    bad = years == 2016
    p[bad] = [1.0, 0.0, 0.0]
    df = _mk_df(obs_all, p, year=years)
    y = years_block(df, "seasonal", "t2m")
    assert y["n_years"] == 20
    assert [r["year"] for r in y["rows"]] == list(range(2005, 2025))
    assert y["positive_years"] == 19
    assert y["worst"]["year"] == 2016 and y["worst"]["rpss"] < 0
    assert y["best"]["year"] == 2005
    assert y["first_decade_rpss"] == pytest.approx(1.0)
    assert y["second_decade_rpss"] == pytest.approx(0.775, abs=1e-4)
    y2005 = next(r for r in y["rows"] if r["year"] == 2005)
    assert y2005["n"] == 15 and y2005["rpss"] == pytest.approx(1.0)


def test_decomposition_greedy_climate_math():
    obs = np.array([0, 1, 2] * 30)
    p = np.eye(3)[obs]
    bp = np.tile([0.0, 0.0, 1.0], (90, 1))
    df = _mk_df(obs, p, bp=bp)
    d = decomposition(df, "seasonal", "t2m")
    assert d["uniform"]["rps"] == pytest.approx(0.4444, abs=1e-5)
    assert d["uniform"]["rpss"] == pytest.approx(0.0)
    assert d["greedy_warm"]["rps"] == pytest.approx(1.0)
    assert d["greedy_warm"]["rpss"] == pytest.approx(1 - 1 / (4 / 9))
    assert d["blend"]["rps"] == pytest.approx(1.0)
    assert d["product"]["rps"] == pytest.approx(0.0)
    assert d["product"]["rpss"] == pytest.approx(1.0)
    obs_above = np.full(30, 2)
    df2 = _mk_df(obs_above, np.eye(3)[obs_above], bp=np.tile([0.0, 0.0, 1.0], (30, 1)))
    d2 = decomposition(df2, "seasonal", "t2m")
    assert d2["greedy_warm"]["rps"] == pytest.approx(0.0)
    assert d2["greedy_warm"]["rpss"] == pytest.approx(1.0)


def test_ece_math_exact():
    obs = np.array([0, 0, 1, 1, 2])
    p = np.array([[1, 0, 0], [1, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]])
    df = _mk_df(obs, p)
    s = segment_stats(df, "seasonal", "t2m")
    assert s["n"] == 5
    assert s["ece"] == pytest.approx(0.24 + 0.04 + 0.16)
    assert s["freq"]["below"] == pytest.approx(0.4)
    assert s["freq"]["normal"] == pytest.approx(0.4)
    assert s["freq"]["above"] == pytest.approx(0.2)
    assert s["claimed"]["below"] == pytest.approx(0.6)
    assert s["claimed"]["normal"] == pytest.approx(0.2)
    assert s["claimed"]["above"] == pytest.approx(0.2)
    assert s["ece_parts"]["below"] == pytest.approx(0.24)
    assert s["ece_parts"]["normal"] == pytest.approx(0.04)
    assert s["ece_parts"]["above"] == pytest.approx(0.16)
    assert s["coverage"] == pytest.approx(1.0)


def test_write_outputs_md_and_json(tmp_path):
    obs, p = _perfect_balanced()
    df = _mk_df(obs, p)
    rep = build_report(df)
    md_p = tmp_path / "value_report.md"
    js_p = tmp_path / "value_report.json"
    write_outputs(rep, md_p, js_p)
    assert md_p.exists() and js_p.exists()
    md = md_p.read_text(encoding="utf-8")
    assert md.startswith("# Паспорт навыка AgroCast")
    assert "## 1. Сегменты" in md
    assert "## 2. По годам — сезонная t2m" in md
    assert "## 4. Декомпозиция" in md
    assert "жадный климат «всегда выше нормы»" in md
    assert "честно" in md.lower()
    loaded = json.loads(js_p.read_text(encoding="utf-8"))
    assert loaded["n_points"] == 28
    assert loaded["verifications"] == 300
    assert loaded["segments"]["seasonal_t2m"]["rpss"] == pytest.approx(1.0)
    assert loaded["decomposition"]["seasonal_t2m"]["product"]["rpss"] == pytest.approx(1.0)
    assert "| 2010 | 300 |" in md
    assert "| сегмент | n | RPS |" in md


def test_value_api_ok_missing_and_page(tmp_path, monkeypatch):
    import agrocast.serve.product as product_mod

    monkeypatch.setattr(product_mod, "WORLD", str(tmp_path))
    miss = value_api()
    assert miss["ok"] is False
    assert "python -m scripts.value_report" in miss["error"]
    art = tmp_path / "artifacts"
    art.mkdir()
    (art / "value_report.json").write_text(
        json.dumps({"title": "Паспорт навыка AgroCast", "n_points": 28,
                    "segments": {"seasonal_t2m": {"rpss": 0.2544}}}),
        encoding="utf-8")
    got = value_api()
    assert got["ok"] is True
    assert got["report"]["n_points"] == 28
    assert got["report"]["segments"]["seasonal_t2m"]["rpss"] == pytest.approx(0.2544)
    body = value_page()
    assert "Паспорт навыка" in body
    assert "fetch('/api/value')" in body
