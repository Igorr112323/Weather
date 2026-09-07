import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from agrocast.backtest import metrics as M  # noqa: E402
from agrocast.blend.conformal import ConformalQuantileCalibrator, _fs_quantile  # noqa: E402


def _perfect_calib(n=60000, seed=4):
    rng = np.random.default_rng(seed)
    P = rng.dirichlet([1.0, 1.0, 1.0], size=n)
    u = rng.random(n)
    cum = np.cumsum(P, axis=1)
    obs = (u[:, None] > cum).sum(axis=1)
    return P, obs.astype(int)


def test_perfect_calibration_gives_near_zero_ece_everywhere():
    P, obs = _perfect_calib()
    e = M.ece(P, obs)
    assert e["top_label"] is not None and abs(e["top_label"]) < 0.015
    assert e["classwise"] is not None and abs(e["classwise"]) < 0.015
    assert e["n_bins"] == M.ECE_BINS and len(e["bin_counts"]) == M.ECE_BINS

    months = np.tile(np.array([3, 6, 9]), int(np.ceil(len(P) / 3)))[: len(P)]
    frame = pd.DataFrame(
        {
            "cell_id": "C1", "cell_lat": 45.0, "cell_lon": 38.0,
            "variable": "t2m", "lead": 1, "mode": "monthly",
            "target_start": [f"20{i % 18 + 4:02d}-03" for i in range(len(P))],
            "year": 2004 + (np.arange(len(P)) % 18),
            "p0": P[:, 0], "p1": P[:, 1], "p2": P[:, 2],
            "q10": -1.28, "q50": 0.0, "q90": 1.28,
            "obs_z": 0.1, "obs_tercile": obs, "mu": 0.0, "sd": 1.0,
        }
    )
    from agrocast.skill import spatial

    combos = spatial.summarize(spatial.add_cell_frame(frame, 45.0, 38.0, "C1"), modes=["monthly"],
                               leads={"monthly": [1]}, variables=("t2m",), n_boot=40, seed=1)
    c = combos["monthly_t2m_l1"]
    assert c["ece_top_label"] == pytest.approx(e["top_label"], abs=1e-9)
    assert c["ece_classwise"] == pytest.approx(e["classwise"], abs=1e-9)

    from agrocast.skill.ledger import _block

    g = frame.copy()
    lb = _block(g)
    assert lb["ece"] == pytest.approx(e["top_label"], abs=1e-3)
    assert lb["ece_classwise"] == pytest.approx(e["classwise"], abs=1e-3)

    import audit_full

    adf = frame.copy()
    adf["hit"] = (P.argmax(axis=1) == obs).astype(int)
    adf["bhit"] = 0
    adf["bp0"], adf["bp1"], adf["bp2"] = 1 / 3, 1 / 3, 1 / 3
    adf["brps"] = M.rps_rows(np.column_stack([adf.bp0, adf.bp1, adf.bp2]), obs)
    adf["rps"] = M.rps_rows(P, obs)
    adf["rps_c"] = M.rps_rows(M.tercile_baseline(obs, months), obs)
    adf["in_corridor"] = 1
    gm = audit_full.group_metrics(adf)
    assert float(gm.iloc[0]["ece"]) == pytest.approx(e["classwise"], abs=5e-4)


def test_rpss_single_definition_and_baseline_choice():
    rng = np.random.default_rng(2)
    n = 3000
    months = rng.integers(1, 13, n)
    base = M.tercile_baseline(np.repeat([0, 1, 2], [n // 2, n // 4, n - n // 2 - n // 4]), months)
    perfect = M.rpss(base, np.repeat([0, 1, 2], [n // 2, n // 4, n - n // 2 - n // 4]), months=months)
    assert abs(perfect) < 1e-12, "прогноз, равный эмпирическому бейзлайну, обязан давать RPSS≈0 у нового определения"

    obs = rng.integers(0, 3, n)
    r_emp = M.rpss(np.full((n, 3), 1 / 3), obs, months=months, baseline="empirical")
    r_uni = M.rpss(np.full((n, 3), 1 / 3), obs, months=months, baseline="uniform")
    assert abs(r_uni) < 1e-12, "равномерный прогноз против равномерного бейзлайна — 0"
    assert abs(r_emp) > 1e-9, "эмпирический бейзлайн отличается от равномерного на перекосе"
    with pytest.raises(ValueError):
        M.rpss(np.full((n, 3), 1 / 3), obs, baseline="bogus")


def test_wilson_reference_values():
    lo, hi = M.wilson_interval(80, 100)
    p, n, z = 0.8, 100, 1.96
    d = 1 + z * z / n
    c = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    assert lo == pytest.approx((p + z * z / (2 * n) - c) / d)
    assert hi == pytest.approx((p + z * z / (2 * n) + c) / d)
    assert lo < 0.8 < hi
    lo0, hi0 = M.wilson_interval(0, 10)
    assert lo0 == 0.0 and hi0 > 0
    assert M.wilson_interval(0, 0) == [None, None]


def test_promotion_rule_exact():
    assert M.promotion_decision(0.03, [0.01, 0.07])["promoted"] is True
    assert M.promotion_decision(0.03, [-0.01, 0.07])["promoted"] is False
    assert M.promotion_decision(0.03, None)["promoted"] is False
    assert M.promotion_decision(0.0, [0.01, 0.07])["promoted"] is False
    d = M.promotion_decision(0.03, [0.01, 0.07])
    assert "RPSS > 0" in d["rule"] and "95%" in d["rule"]


def test_block_bootstrap_ci_shape_and_degenerate():
    st = M.block_bootstrap_ci([0.5] * 50, 12, 100, 3)
    assert st["ci95"][0] == pytest.approx(0.5) and st["ci95"][1] == pytest.approx(0.5)
    st2 = M.block_bootstrap_ci(np.arange(30, dtype=float), 12, 200, 5)
    assert st2["n_blocks"] == 3
    lo, hi = st2["ci95"]
    assert lo <= hi
    a = M.block_bootstrap_ci(np.arange(30, dtype=float), 12, 200, 5)
    b = M.block_bootstrap_ci(np.arange(30, dtype=float), 12, 200, 5)
    c = M.block_bootstrap_ci(np.arange(30, dtype=float), 12, 200, 6)
    assert a["ci95"] == b["ci95"]
    assert a["ci95"] != c["ci95"]


def test_fs_quantile_is_conservative():
    x = np.arange(20, dtype=float)
    assert _fs_quantile(x, 0.1, "low") <= float(np.quantile(x, 0.1))
    assert _fs_quantile(x, 0.9, "high") >= float(np.quantile(x, 0.9))
    assert _fs_quantile(np.arange(5, dtype=float), 0.5, "mid") == pytest.approx(2.0)


def _calib_records(n=900, seed=0):
    rng = np.random.default_rng(seed)
    obs = rng.normal(0, 1, n)
    q50 = obs - 0.5 + rng.normal(0, 0.3, n)
    return pd.DataFrame(
        {
            "variable": "t2m", "lead": rng.integers(1, 7, n),
            "year": 2000 + np.arange(n) % 20,
            "q10": q50 - 0.6, "q50": q50, "q90": q50 + 0.6, "obs_z": obs,
        }
    )


def test_coverage_requires_out_of_fit_holdout():
    rec = _calib_records()
    ccal = ConformalQuantileCalibrator().fit(rec)
    assert ccal.fit_years["t2m"] == sorted(set(int(y) for y in rec["year"]))
    with pytest.raises(ValueError, match="вне fit/calibration"):
        ccal.coverage80(rec)
    hold = rec.copy()
    hold["year"] = 2099
    res = ccal.coverage80(hold)
    assert res["n"] == len(rec)
    lo, hi = res["coverage_wilson95"]
    assert lo <= res["p80_coverage"] <= hi
    assert 0.0 < res["mean_width_z"] < 3.0
    assert 0.0 <= res["p80_coverage"] <= 1.0
    assert res["finite_sample_correction"] is True
    assert "80%" in res["note"]


def test_conformal_fit_years_roundtrip(tmp_path):
    ccal = ConformalQuantileCalibrator().fit(_calib_records())
    path = tmp_path / "c.json"
    ccal.save(path)
    got = json.loads(path.read_text())
    assert got["fit_years"]["t2m"]
    c2 = ConformalQuantileCalibrator.load(path)
    assert c2.fit_years == ccal.fit_years
    with pytest.raises(ValueError):
        c2.coverage80(_calib_records())


def test_spatial_combos_publish_promo_ece_and_width():
    sys.path.insert(0, str(ROOT / "tests"))
    from test_spatial_skill import _toy_pipe
    from agrocast.skill import spatial

    combos = spatial.summarize(_toy_pipe(), modes=["monthly"], leads={"monthly": [1]}, n_boot=120, seed=9)
    c = combos["monthly_t2m_l1"]
    for key in ("rpss", "rpss_ci95_time", "skill_promoted", "promotion_rule", "ece_top_label", "ece_classwise",
                "width80_z", "fit_exclusion", "n_time_blocks", "n_spatial_blocks"):
        assert key in c, key
    lo, hi = c["rpss_ci95_time"]
    assert M.promotion_decision(c["rpss"], c["rpss_ci95_time"])["promoted"] == c["skill_promoted"]
    assert c["width80_z"] == pytest.approx(2.56)
    assert c["fit_exclusion"]["source"] == "none"


def test_spatial_fit_exclusion_uses_artifacts(tmp_path):
    sys.path.insert(0, str(ROOT / "tests"))
    from test_spatial_skill import _toy_pipe
    from agrocast.skill import spatial

    rng = np.random.default_rng(0)
    obs = rng.normal(0, 1, 500)
    q50 = obs - 0.3
    fit = pd.DataFrame({"variable": "t2m", "lead": 1, "year": 2018 + np.arange(500) % 3,
                        "q10": q50 - 0.5, "q50": q50, "q90": q50 + 0.5, "obs_z": obs})
    ccal = ConformalQuantileCalibrator().fit(fit)
    ccal.save(tmp_path / "conformal_monthly_t2m.json")

    pipe = _toy_pipe()
    plain = spatial.summarize(pipe, modes=["monthly"], leads={"monthly": [1]}, n_boot=80, seed=4)
    excl = spatial.summarize(pipe, modes=["monthly"], leads={"monthly": [1]}, n_boot=80, seed=4, artifact_dir=tmp_path)
    assert excl["monthly_t2m_l1"]["fit_exclusion"]["n_excluded_fit_overlap"] == 18
    assert excl["monthly_t2m_l1"]["n_rows"] == 6
    assert plain["monthly_t2m_l1"]["fit_exclusion"]["n_excluded_fit_overlap"] == 0


def test_ledger_and_engine_share_definition():
    from agrocast.skill import spatial

    assert spatial.tercile_baseline is M.tercile_baseline
    assert spatial.block_bootstrap_ci is M.block_bootstrap_ci
    assert spatial.rps_rows is M.rps_rows
    assert spatial.ece_metric is M.ece


def test_promotion_contradiction_gate():
    import audit_full

    assert audit_full.promotion_contradiction({"skill_promoted": False}, None) is None
    assert audit_full.promotion_contradiction({"skill_promoted": True}, {"promoted": True}) is None
    msg = audit_full.promotion_contradiction({"skill_promoted": False, "rpss": 0.01, "rpss_ci95_time": [-0.1, 0.2]},
                                             {"promoted": True, "schema": "grid-skill-v2", "region": "krai"})
    assert msg and "противоречие" in msg
    assert "нечитаем" in audit_full.promotion_contradiction({}, {"error": "артефакт навыка нечитаем: x"})
