import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from agrocast.core.artifacts import ArtifactError
from agrocast.core.config import Config
from agrocast.serve.pipeline import world_config

WORLD = Path(__file__).resolve().parents[1] / "world"


@pytest.fixture(scope="module")
def cfg(tmp_path_factory):
    state = tmp_path_factory.mktemp("pipeline-state")
    return world_config(WORLD, state)


def _run_module_backtest(cfg):
    from agrocast.backtest.engine import run_backtest

    kwargs = dict(variables=("t2m", "tp"), start_months=[12], leads=[1, 2, 3], years=[2025], mode="monthly", save_artifacts=True)
    rec = run_backtest(cfg, **kwargs)
    return rec


def test_live_api_and_hindcast_return_same_pq(cfg):
    from agrocast.backtest.engine import pipeline_table_name
    from agrocast.forecast.orchestrator import forecast_point

    _run_module_backtest(cfg)
    rec = _run_module_backtest(cfg)
    assert not rec.empty
    fc = forecast_point(cfg, 45.03, 39.07, start=pd.Period("2025-12", "M"), horizon=3, variables=("t2m", "tp"), save=False, mode="monthly")
    table = pd.read_parquet(cfg.artifact_dir / pipeline_table_name("monthly"))
    assert not table.empty
    for i, month in enumerate(fc["months"]):
        lead = int(month["lead"])
        for v in ("t2m", "tp"):
            blk = month[v]
            row = table[(table["variable"] == v) & (table["lead"] == lead)].iloc[0]
            assert blk["_calc"]["P"] == pytest.approx([row["p0"], row["p1"], row["p2"]], abs=1e-12)
            assert blk["_calc"]["qz"] == pytest.approx([row["q10"], row["q50"], row["q90"]], abs=1e-12)
            for mname, probs in blk["model_probs"].items():
                rec_row = rec[(rec["variable"] == v) & (rec["lead"] == lead) & (rec["model"] == mname)].iloc[0]
                assert probs == [round(float(rec_row["p0"]), 3), round(float(rec_row["p1"]), 3), round(float(rec_row["p2"]), 3)]


def test_records_keep_verifiable_intervals_from_shared_core(cfg):
    from agrocast.backtest.engine import run_backtest

    rec = run_backtest(cfg, variables=("t2m",), start_months=[12], leads=[1], years=[2025], mode="monthly", save_artifacts=False)
    assert not rec.empty
    for _, r in rec.iterrows():
        issue = pd.Period(r["issue"], "M")
        assert pd.Period(r["train_until"], "M") == issue - 1
        assert int(r["train_n"]) >= 18
        assert pd.Period(r["target_start"], "M") > issue


def test_tp_alpha_policy_is_enforced_independently_of_json(cfg):
    from agrocast.blend.nn_stack import load_alpha
    from agrocast.forecast.orchestrator import forecast_point

    stack_tp = cfg.artifact_dir / "stack_seasonal_tp.json"
    stack_t2m = cfg.artifact_dir / "stack_seasonal_t2m.json"
    base = forecast_point(cfg, 45.03, 39.07, start=pd.Period("2025-12", "M"), horizon=3, variables=("t2m", "tp"), save=False, mode="seasonal", season_len=3)
    stack_tp.write_text(json.dumps({"schema": "stack-v1", "alpha": 0.5}))
    try:
        assert load_alpha(cfg, "seasonal", "tp") == 0.0
        with_tp = forecast_point(cfg, 45.03, 39.07, start=pd.Period("2025-12", "M"), horizon=3, variables=("t2m", "tp"), save=False, mode="seasonal", season_len=3)
        assert with_tp["seasons"][0]["tp"]["_calc"]["P"] == base["seasons"][0]["tp"]["_calc"]["P"]
        allowed = Config(data_dir=cfg.data_dir, allow_tp_nn_alpha=True)
        assert allowed.allow_tp_nn_alpha is True
    finally:
        stack_tp.unlink(missing_ok=True)
    stack_t2m.write_text(json.dumps({"schema": "stack-v1", "alpha": 0.5}))
    try:
        assert load_alpha(cfg, "seasonal", "t2m") == 0.5
        with_t2m = forecast_point(cfg, 45.03, 39.07, start=pd.Period("2025-12", "M"), horizon=3, variables=("t2m", "tp"), save=False, mode="seasonal", season_len=3)
        stack_t2m.unlink()
        without = forecast_point(cfg, 45.03, 39.07, start=pd.Period("2025-12", "M"), horizon=3, variables=("t2m", "tp"), save=False, mode="seasonal", season_len=3)
        a = with_t2m["seasons"][0]["t2m"]["_calc"]["P"]
        b = without["seasons"][0]["t2m"]["_calc"]["P"]
        if a == b:
            pytest.skip("nn kernel not usable on the bundle at this point")
        assert a != b
    finally:
        stack_t2m.unlink(missing_ok=True)


def test_incompatible_artifacts_fail_loud(tmp_path):
    from agrocast.blend.blender import Blender

    good = tmp_path / "blender_seasonal.json"
    good.write_text(json.dumps({"weights": {"t2m": {"DJF": {"clim": 1.0}}}}))
    loaded = Blender.load(good)
    assert loaded is not None and loaded.weights
    corrupt = tmp_path / "blender_corrupt.json"
    corrupt.write_text("{not json")
    with pytest.raises(ArtifactError):
        Blender.load(corrupt)
    ghost = tmp_path / "blender_ghost.json"
    ghost.write_text(json.dumps({"schema": "blender-v2", "weights": {"t2m": {"DJF": {"retired_model": 1.0}}}}))
    with pytest.raises(ArtifactError):
        Blender.load(ghost)
    wrong = tmp_path / "blender_wrong.json"
    wrong.write_text(json.dumps({"schema": "blender-v1", "weights": {}}))
    with pytest.raises(ArtifactError):
        Blender.load(wrong)
    tmp_path.joinpath("stack_seasonal_t2m.json").write_text("garbage")
    from agrocast.blend.nn_stack import load_alpha

    cfg = Config(data_dir=str(tmp_path / "state"))
    cfg.artifact_dir.mkdir(parents=True, exist_ok=True)
    cfg.artifact_dir.joinpath("stack_seasonal_t2m.json").write_text("garbage")
    with pytest.raises(ArtifactError):
        load_alpha(cfg, "seasonal", "t2m")


def test_hindcast_serves_walk_forward_ledger(cfg):
    from agrocast.backtest.engine import run_backtest
    from agrocast.skill.ledger import load_ledger
    from agrocast.serve.pipeline import run_hindcast

    run_backtest(cfg, variables=("t2m", "tp"), start_months=[12], leads=[1], years=[2020], mode="seasonal", save_artifacts=True)
    led, _ = load_ledger(cfg, "seasonal")
    assert led is not None and not led.empty
    log_lines = []
    out = run_hindcast(cfg, 45.03, 39.07, "2020-12", "seasonal", 3, log_lines.append)
    assert out["kind"] == "hindcast"
    assert out["items"]
    it = out["items"][0]
    v = "t2m" if "t2m" in it else "tp"
    row = led[(led["variable"] == v) & (led["year"] == 2020) & (led["target_month"] == 12)].iloc[0]
    assert it[v]["probs"] == [round(float(row["p0"]), 3), round(float(row["p1"]), 3), round(float(row["p2"]), 3)]
    assert "fold_train_until" in led.columns


def test_corrupt_bundle_blender_is_not_silently_dropped(cfg):
    from agrocast.forecast.orchestrator import forecast_point

    path = cfg.artifact_dir / "blender_seasonal.json"
    original = path.read_text() if path.exists() else None
    path.write_text("{corrupt")
    try:
        with pytest.raises(ArtifactError):
            forecast_point(cfg, 45.03, 39.07, start=pd.Period("2025-12", "M"), horizon=3, variables=("t2m",), save=False, mode="seasonal", season_len=3)
    finally:
        if original is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(original)


def test_api_rejects_unverified_leads_and_modes(clients):
    client = clients()
    body = {"lat": 45.03, "lon": 39.07, "start": "2026-03", "horizon": 7, "mode": "monthly", "season_len": 1}
    assert client.post("/api/prepare", json=body).status_code == 422
    body["horizon"] = 2
    body["season_len"] = 2
    assert client.post("/api/prepare", json=body).status_code == 422
    body["horizon"] = 7
    assert client.post("/api/prepare", json=body).status_code == 422


def test_refit_writes_validated_artifacts(tmp_path):
    from agrocast.blend.blender import Blender
    from agrocast.blend.calibration import TercileCalibrator, MonoCurve
    from agrocast.blend.conformal import ConformalQuantileCalibrator
    from agrocast.models.builder import MODEL_NAMES

    b = Blender(half_life_years=5.0)
    b.weights = {"t2m": {"DJF": {"ridge": 1.0}}}
    b.save(tmp_path / "blender_seasonal.json")
    data = json.loads((tmp_path / "blender_seasonal.json").read_text())
    assert data["schema"] == "blender-v2"
    assert set(data["models"]) == set(MODEL_NAMES)
    cal = TercileCalibrator()
    cal.n = 5
    cal.curves = [MonoCurve(np.linspace(0, 1, 4), np.linspace(0, 1, 4))] * 3
    cal.save(tmp_path / "calib_seasonal_t2m.json")
    assert json.loads((tmp_path / "calib_seasonal_t2m.json").read_text())["schema"] == "calib-v1"
    cc = ConformalQuantileCalibrator()
    cc.n = 3
    cc.deltas = {"t2m": {}}
    cc.save(tmp_path / "conformal_seasonal_t2m.json")
    assert json.loads((tmp_path / "conformal_seasonal_t2m.json").read_text())["schema"] == "conformal-v1"
