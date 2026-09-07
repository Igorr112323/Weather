import json
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from fastapi.testclient import TestClient

from agrocast.core.config import Config, Region
from agrocast.core.errors import IssueFreshnessError
from agrocast.core.settings import RuntimeSettings
from agrocast.forecast.orchestrator import align_issue
from agrocast.region.regions import REGIONS
from agrocast.serve import readiness
from agrocast.serve.product import create_app
from agrocast.store.zarrstore import ZarrStore

NOW = pd.Timestamp("2026-09-06")
END = pd.Timestamp("2026-09-05")


def make_ds(complete=True):
    index = pd.date_range(end=END, periods=8, freq="D")
    values = np.ones(8)
    if not complete:
        values[-1] = np.nan
    return xr.Dataset({"value": (("time",), values)}, coords={"time": index})


def write_bundle(world, *, missing=(), incomplete=(), ready=True, contract=None, predictor_through=None,
                 skill="ok", grid="ok"):
    import shutil

    from agrocast.region.regions import grid_artifact_path, skill_artifact_path

    shutil.rmtree(world, ignore_errors=True)
    world.mkdir(parents=True)
    Config(data_dir=str(world), region=Region(), random_state=73).save(world / "config.json")
    store = ZarrStore(world / "zarr")
    for name in (*readiness.REQUIRED_SOURCES, *readiness.OPTIONAL_SOURCES):
        if name in missing:
            continue
        store.write(name, make_ds(complete=name not in incomplete))
    ready_doc = {"ok": True}
    if ready:
        if contract is not None:
            ready_doc["contract"] = contract
        if predictor_through is not None:
            ready_doc["predictor_through"] = predictor_through
        (world / "ready.json").write_text(json.dumps(ready_doc))
    else:
        (world / "ready.json").write_text("{broken")
    for region_id in sorted(REGIONS):
        grid_path = grid_artifact_path(world, region_id)
        skill_path = skill_artifact_path(world, region_id)
        grid_path.parent.mkdir(parents=True, exist_ok=True)
        if grid == "ok":
            grid_path.write_text(json.dumps({"name": region_id}))
        else:
            grid_path.write_text("{broken")
        if skill == "missing":
            continue
        payload = {"region": region_id}
        if skill == "ok":
            payload["generated_at"] = str(END.date())
        elif skill == "undated":
            pass
        elif skill == "stale":
            payload["generated_at"] = str((NOW - pd.Timedelta(days=500)).date())
        else:
            payload["generated_at"] = 5 * 10 ** 15
        skill_path.write_text(json.dumps(payload))


def settings_for(tmp_path, name="world", **kwargs):
    world = tmp_path / name
    write_bundle(world, **kwargs)
    return RuntimeSettings(world_dir=world, state_dir=tmp_path / (name + "-state"), public_origin="https://testserver")


def evaluate(settings, engine=None):
    return readiness.evaluate(settings, settings.compute_config(), engine=engine, now=NOW)


def test_all_fresh_is_ready(tmp_path):
    result = evaluate(settings_for(tmp_path))
    assert result["status"] == "ready"
    assert result["reasons"] == []
    assert result["degraded"] == []
    assert result["sources"]["daily_region"]["status"] == "ok"
    assert result["sources"]["daily_region"]["available_at"] == str(END.date())
    assert result["queue"]["status"] == "unavailable"


def test_missing_required_store_blocks_readiness(tmp_path):
    result = evaluate(settings_for(tmp_path, missing=("daily_region",)))
    assert result["status"] == "not_ready"
    assert "daily_region:missing" in result["reasons"]


def test_optional_source_gives_marked_degradation(tmp_path):
    result = evaluate(settings_for(tmp_path, missing=("sst",)))
    assert result["status"] == "ready"
    assert "sst:missing" in result["degraded"]


def test_stale_required_store_blocks_readiness(tmp_path):
    settings = settings_for(tmp_path)
    store = ZarrStore(settings.world_dir / "zarr")
    old = pd.date_range(end=END - pd.Timedelta(days=200), periods=8, freq="D")
    store.write("fields_monthly", xr.Dataset({"value": (("time",), np.ones(8))}, coords={"time": old}))
    result = evaluate(settings)
    assert result["status"] == "not_ready"
    assert "fields_monthly:stale" in result["reasons"]


def test_incomplete_last_row_blocks_readiness(tmp_path):
    result = evaluate(settings_for(tmp_path, incomplete=("daily_region",)))
    assert result["status"] == "not_ready"
    assert "daily_region:incomplete_last_row" in result["reasons"]


@pytest.mark.parametrize("kwargs,tag", [
    ({"ready": False}, "bundle:corrupt"),
    ({"contract": "world-v0"}, "bundle:contract_mismatch"),
    ({"predictor_through": str((END + pd.Timedelta(days=20)).date())}, "bundle:manifest_ahead_of_store"),
])
def test_bundle_defects_block_readiness(tmp_path, kwargs, tag):
    settings = settings_for(tmp_path, **kwargs)
    result = evaluate(settings)
    assert result["status"] == "not_ready"
    assert tag in result["reasons"]


def test_missing_ready_file_blocks_readiness(tmp_path):
    settings = settings_for(tmp_path)
    (settings.world_dir / "ready.json").unlink()
    result = evaluate(settings)
    assert "bundle:missing" in result["reasons"]


def test_region_artifact_rules(tmp_path):
    result = evaluate(settings_for(tmp_path, "variant-a", skill="missing"))
    assert "region/krai:skill_missing" in result["reasons"]
    result = evaluate(settings_for(tmp_path, "variant-b", skill="undated"))
    assert result["status"] == "ready"
    assert "region/krai:skill_undated" in result["degraded"]
    result = evaluate(settings_for(tmp_path, "variant-c", skill="stale"))
    assert "region/krai:skill_stale" in result["degraded"]
    result = evaluate(settings_for(tmp_path, "variant-d", grid="broken"))
    assert "region/krai:grid_corrupt" in result["reasons"]


def test_database_failure_blocks_readiness(tmp_path):
    def boom():
        raise RuntimeError("connection reset")

    result = evaluate(settings_for(tmp_path), engine=SimpleNamespace(connect=boom))
    assert result["status"] == "not_ready"
    assert "db_unavailable" in result["reasons"]
    assert result["queue"]["status"] == "error"


def test_readiness_endpoint_marks_http_status(tmp_path, identity, monkeypatch):
    settings = settings_for(tmp_path)
    with TestClient(create_app(settings=settings, identity=identity)) as client:
        response = client.get("/health/ready")
        assert response.status_code == 200
        assert response.json()["status"] == "ready"
        import shutil

        shutil.rmtree(settings.world_dir / "zarr" / "daily_region")
        response = client.get("/health/ready")
        assert response.status_code == 503
        assert "daily_region:missing" in response.json()["reasons"]
        assert client.get("/health/live").status_code == 200


def test_desktop_state_database_satisfies_queue_block(tmp_path):
    settings = settings_for(tmp_path)
    from sqlalchemy import create_engine

    engine = create_engine("sqlite://")
    with engine.connect() as connection:
        from sqlalchemy import text

        connection.execute(text("CREATE TABLE jobs (status TEXT, created_at BIGINT)"))
        connection.execute(text("INSERT INTO jobs VALUES ('queued', 1)"))
        connection.commit()
    result = readiness.evaluate(settings, settings.compute_config(), engine=engine, now=NOW)
    assert result["queue"]["status"] == "ok"
    assert result["queue"]["jobs"] == {"queued": 1}


def test_align_issue_anchors_to_inputs():
    index = pd.period_range("2026-01", "2026-08", freq="M")
    start, issue = align_issue(pd.Period("2026-05", "M"), index)
    assert (start, issue) == (pd.Period("2026-05", "M"), pd.Period("2026-04", "M"))
    start, issue = align_issue(None, index)
    assert issue == index[-1]
    assert start == issue + 1


def test_align_issue_refuses_future_target_on_old_inputs():
    february_end = pd.period_range("2026-01", "2026-02", freq="M")
    with pytest.raises(IssueFreshnessError):
        align_issue(pd.Period("2026-10", "M"), february_end)
    hole = pd.period_range("2026-01", "2026-08", freq="M").drop(pd.Period("2026-04", "M"))
    with pytest.raises(IssueFreshnessError):
        align_issue(pd.Period("2026-05", "M"), hole)


def test_limits_are_consistent_with_update_schedule():
    names = (*readiness.REQUIRED_SOURCES, *readiness.OPTIONAL_SOURCES)
    assert set(readiness.SOURCE_LIMITS) == set(names)
    assert not set(readiness.REQUIRED_SOURCES) & set(readiness.OPTIONAL_SOURCES)
    assert all(limit >= readiness.UPDATE_SCHEDULE_DAYS for limit in readiness.SOURCE_LIMITS.values())
    assert readiness.SKILL_STALE_DAYS >= readiness.UPDATE_SCHEDULE_DAYS


def test_freshness_script_reuses_readiness_and_alerts_on_missing(tmp_path):
    import importlib.util
    from pathlib import Path as FilePath

    spec = importlib.util.spec_from_file_location(
        "zarr_freshness_under_test", FilePath(__file__).resolve().parents[1] / "scripts" / "zarr_freshness.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    settings = settings_for(tmp_path)
    assert module.run(settings, now=NOW) == []
    import shutil

    shutil.rmtree(settings.world_dir / "zarr" / "daily_region")
    assert module.run(settings, now=NOW) == ["daily_region:missing"]
