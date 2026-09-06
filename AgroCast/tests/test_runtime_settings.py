import json
import logging
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agrocast.core.config import Config, Region
from agrocast.core.settings import ConfigurationError, RuntimeSettings
from agrocast.serve import cli, pipeline, product, region
from agrocast.store.atomic import write_json


@pytest.fixture
def isolated_settings(tmp_path):
    world = tmp_path / "world"
    world.mkdir()
    Config(data_dir=str(world), region=Region(), random_state=73).save(world / "config.json")
    (world / "artifacts").mkdir()
    (world / "ready.json").write_text('{"ok":true}')
    return RuntimeSettings(world_dir=world, state_dir=tmp_path / "state", public_origin="https://testserver")


@pytest.mark.parametrize("name,value", [
    ("AGROCAST_WORLD_DIR", "relative"), ("AGROCAST_STATE_DIR", "relative"),
    ("AGROCAST_LOG_LEVEL", "INVALID"), ("AGROCAST_SESSION_SECONDS", "True"),
    ("AGROCAST_SESSION_SECONDS", "299"), ("AGROCAST_SESSION_SECONDS", "86401"),
    ("AGROCAST_PHYS_PRESET", "other"), ("AGROCAST_REGIME_GUARD", "yes"),
    ("AGROCAST_OSPR", "None"), ("AGROCAST_OSPR_W", "nan"), ("AGROCAST_OSPR_W", "inf"),
    ("AGROCAST_OSPR_W", "-1"), ("AGROCAST_OSPR_W", "1.01"), ("AGROCAST_OSPR_W", "secret-value"),
    ("AGROCAST_PUBLIC_ORIGIN", "http://example.com"), ("AGROCAST_PUBLIC_ORIGIN", "https://user:secret@example.com"),
])
def test_bad_environment_is_rejected_without_echoing_values(name, value):
    with pytest.raises(ConfigurationError) as failure:
        RuntimeSettings.from_environment({name: value})
    assert "secret" not in str(failure.value)


@pytest.mark.parametrize("canonical,alias", [("AGROCAST_WORLD_DIR", "AGROCAST_WORLD"), ("AGROCAST_STATE_DIR", "AGROCAST_DATA"), ("AGROCAST_STATE_DIR", "AGROCAST_DATA_DIR"), ("AGROCAST_PHYS_PRESET", "PHYS_PRESET"), ("AGROCAST_REGIME_GUARD", "REGIME_GUARD")])
def test_alias_conflicts_are_not_silently_resolved(canonical, alias, tmp_path):
    with pytest.raises(ConfigurationError, match="Conflicting"):
        RuntimeSettings.from_environment({canonical: str(tmp_path / "one"), alias: str(tmp_path / "two")})


@pytest.mark.parametrize("state", ["world", "world/state", "."])
def test_bundle_and_state_are_disjoint(tmp_path, state):
    with pytest.raises(ConfigurationError, match="overlap"):
        RuntimeSettings(world_dir=tmp_path / "world", state_dir=tmp_path / state)


def test_settings_api_cli_and_process_worker_share_exact_configuration(isolated_settings, identity, monkeypatch, capsys):
    settings = isolated_settings
    for key, value in {"AGROCAST_WORLD_DIR": settings.world_dir, "AGROCAST_STATE_DIR": settings.state_dir, "AGROCAST_PHYS_PRESET": "state", "AGROCAST_REGIME_GUARD": "0", "AGROCAST_OSPR": "0", "AGROCAST_OSPR_W": "0.45", "AGROCAST_PUBLIC_ORIGIN": settings.public_origin}.items():
        monkeypatch.setenv(key, str(value))
    settings = RuntimeSettings.from_environment()
    expected = settings.compute_config().to_dict()
    assert cli._load_config().to_dict() == expected
    assert pipeline.world_config().to_dict() == expected
    app = product.create_app(identity, settings)
    with TestClient(app):
        assert app.state.config.to_dict() == expected
    cli.main(["settings"])
    assert json.loads(capsys.readouterr().out) == settings.public_snapshot()
    monkeypatch.setenv("AGROCAST_OSPR_W", "0.9")
    cfg, _ = pipeline.point_config(settings.world_dir, settings.state_dir, 45.0, 39.0, expected)
    assert cfg.to_dict() == expected
    import agrocast.forecast.orchestrator as orchestrator

    seen = []
    monkeypatch.setattr(pipeline, "ensure_point", lambda *args: None)

    def forecast(config, lat, lon, **kwargs):
        seen.append(config.to_dict())
        return {"start": "2026-10", "issue_data_through": "2026-02", "seasons": [{"year": 2026, "month": 10, "months": ["2026-10", "2026-11", "2026-12"], "tp": {"tercile_probs": {"below": 0.2, "normal": 0.3, "above": 0.5}}}]}

    monkeypatch.setattr(orchestrator, "forecast_point", forecast)
    region._forecast_cell(("P01", 45.0, 39.0, "2026-10", str(settings.world_dir), str(settings.state_dir), expected))
    assert seen == [expected]


def test_config_fallback_reads_bundle_but_writes_only_state(isolated_settings):
    settings = isolated_settings
    settings.prepare_state()
    bundle = settings.world_dir / "artifacts"
    write_json(bundle / "model.json", {"value": "bundle"})
    cfg = settings.compute_config()
    assert cfg.artifact_path("model.json") == bundle / "model.json"
    assert not cfg.artifact_dir.samefile(bundle)
    write_json(cfg.artifact_dir / "model.json", {"value": "state"})
    assert cfg.artifact_path("model.json") == cfg.artifact_dir / "model.json"
    assert json.loads((bundle / "model.json").read_text()) == {"value": "bundle"}
    with pytest.raises(ValueError, match="read-only"):
        cfg.save(settings.world_dir / "config.json")
    with pytest.raises(ValueError, match="escapes"):
        cfg.dir("../escape")
    point, root = pipeline.point_config(settings.world_dir, settings.state_dir, 55.0, 39.0, cfg.to_dict())
    assert Path(root).is_relative_to(settings.state_dir)
    assert point.random_state == 73
    assert not point.use_bundle_models
    assert point.artifact_path("model.json").is_relative_to(settings.state_dir)
    assert point.source_artifact("model.json") == cfg.artifact_dir / "model.json"


def test_bundle_ready_marker_does_not_trigger_hidden_training(isolated_settings, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("no implicit training for existing world bundle")

    monkeypatch.setattr(pipeline, "_fit_artifacts", forbidden)
    pipeline.ensure_point(isolated_settings.compute_config(), isolated_settings.world_dir, lambda text: None)
    assert not isolated_settings.state_dir.exists()


def test_lifespan_owns_logger_and_does_not_change_root_handlers(isolated_settings, identity):
    root_handlers = list(logging.getLogger().handlers)
    app = product.create_app(identity, isolated_settings)
    assert not isolated_settings.state_dir.exists()
    assert not app.state.started
    with TestClient(app) as client:
        assert app.state.started
        assert client.get("/health/live").status_code == 200
        handler = app.state.logger.handlers[0]
    assert not app.state.started
    assert handler not in app.state.logger.handlers
    assert logging.getLogger().handlers == root_handlers


@pytest.mark.parametrize("broken", ["missing_config", "invalid_config", "missing_world", "bad_release", "state_is_file", "state_symlink", "wrong_origin"])
def test_invalid_configuration_prevents_startup(isolated_settings, identity, broken):
    settings = isolated_settings
    if broken == "missing_config":
        (settings.world_dir / "config.json").unlink()
    elif broken == "invalid_config":
        (settings.world_dir / "config.json").write_text('{"private":"do-not-echo"}')
    elif broken == "missing_world":
        settings = replace(settings, world_dir=settings.world_dir.parent / "absent")
    elif broken == "bad_release":
        settings = replace(settings, release_manifest_file=settings.world_dir / "missing-release.json")
    elif broken == "state_is_file":
        settings.state_dir.write_text("blocked")
    elif broken == "state_symlink":
        settings.state_dir.mkdir()
        (settings.state_dir / "compute").symlink_to(settings.world_dir)
    else:
        settings = replace(settings, public_origin="https://wrong.example.com")
    with pytest.raises(ConfigurationError) as failure:
        with TestClient(product.create_app(identity, settings)):
            pytest.fail("startup must fail")
    assert "do-not-echo" not in str(failure.value)


def test_imports_do_not_start_application_open_database_hash_password_or_configure_logging(tmp_path):
    program = '''import logging, pathlib, sqlite3
import sqlalchemy, argon2, fastapi, pandas, xarray, sklearn
root = list(logging.getLogger().handlers)
def forbidden(*args, **kwargs):
    raise AssertionError("import-time side effect")
sqlalchemy.create_engine = forbidden
sqlite3.connect = forbidden
argon2.PasswordHasher.hash = forbidden
logging.basicConfig = forbidden
pathlib.Path.mkdir = forbidden
pathlib.Path.write_text = forbidden
read_text = pathlib.Path.read_text
def guarded_read(self, *args, **kwargs):
    if any(part in {"world", "data", "absent-world", "absent-state", "absent-secret"} for part in self.parts):
        forbidden()
    return read_text(self, *args, **kwargs)
pathlib.Path.read_text = guarded_read
import agrocast.serve.product as product
import agrocast.serve.api
import agrocast.serve.cli
import agrocast.serve.digest
import agrocast.state.cli
import scripts.audit_full
import scripts.krig_demo
import scripts.zarr_freshness
assert not hasattr(product, "app")
assert logging.getLogger().handlers == root
'''
    environment = {key: value for key, value in os.environ.items() if not key.startswith("AGROCAST_")}
    environment.update(AGROCAST_WORLD_DIR=str(tmp_path / "absent-world"), AGROCAST_STATE_DIR=str(tmp_path / "absent-state"), AGROCAST_DATABASE_URL_FILE=str(tmp_path / "absent-secret"), PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run([sys.executable, "-c", program], cwd=Path(__file__).resolve().parents[1], env=environment, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert not list(tmp_path.iterdir())


def test_offline_job_snapshot_survives_new_process_without_resuming(isolated_settings, monkeypatch):
    import agrocast.forecast.orchestrator as orchestrator

    monkeypatch.setattr(pipeline, "ensure_point", lambda *args: None)
    monkeypatch.setattr(orchestrator, "forecast_point", lambda *args, **kwargs: {"saved": True})
    job = pipeline.Job("legacy-job", {"lat": 45.0, "lon": 39.0, "start": "2026-10", "mode": "seasonal", "horizon": 3})
    pipeline.run_job(job, isolated_settings.world_dir, isolated_settings.state_dir)
    snapshot = isolated_settings.state_dir / "offline-jobs" / (job.id + ".json")
    stored = json.loads(snapshot.read_text())
    assert stored["ownership"] == "unassigned_offline"
    assert stored["result"] == {"saved": True}
    assert stored["status"] == "done"
    assert snapshot.stat().st_mode & 0o777 == 0o600
    result = subprocess.run([sys.executable, "-c", "import json,sys; d=json.load(open(sys.argv[1])); print(d['status'])", str(snapshot)], capture_output=True, text=True, timeout=10)
    assert result.stdout.strip() == "done"
    assert job.id not in product.JOBS


def test_point_reads_common_writable_inputs_before_bundle_without_reusing_world_models(isolated_settings):
    import xarray as xr
    from agrocast.store.zarrstore import ZarrStore

    settings = isolated_settings
    dataset = xr.Dataset({"value": ("time", [1.0])}, coords={"time": [1]})
    ZarrStore(settings.world_dir / "zarr").write("shared", dataset)
    world = settings.compute_config()
    world.zarr_store().write("shared", dataset * 2)
    world.zarr_store().write("updated", dataset * 3)
    point, _ = pipeline.point_config(settings.world_dir, settings.state_dir, 55.0, 39.0, world.to_dict())
    store = point.zarr_store()
    assert store.names() == ["shared", "updated"]
    assert store.open("shared")["value"].values.item() == 2
    assert store.open("updated")["value"].values.item() == 3
    store.write("shared", dataset * 4)
    assert store.open("shared")["value"].values.item() == 4
    assert world.zarr_store().open("shared")["value"].values.item() == 2


def test_disabled_state_calibrator_masks_old_bundle_model(isolated_settings):
    from agrocast.blend.calibration import TercileCalibrator

    cfg = isolated_settings.compute_config()
    name = "calib_seasonal_t2m.json"
    write_json(isolated_settings.world_dir / "artifacts" / name, {"n": 500, "curves": []})
    assert TercileCalibrator.load(cfg.artifact_path(name)).usable()
    TercileCalibrator().save(cfg.artifact_dir / name)
    assert not TercileCalibrator.load(cfg.artifact_path(name)).usable()


def test_live_ledger_uses_copy_on_write_for_bundled_history(isolated_settings):
    from agrocast.skill.ledger import append_live, live_summary

    cfg = isolated_settings.compute_config()
    raw = Config(data_dir=str(isolated_settings.world_dir))
    append_live(raw, "2024-01", 45.0, 39.0, "t2m", [0.2, 0.3, 0.5], [1, 2, 3])
    original = raw.artifact_path("live_ledger.parquet").read_bytes()
    append_live(cfg, "2025-01", 45.0, 39.0, "t2m", [0.2, 0.3, 0.5], [2, 3, 4])
    assert len(live_summary(cfg)) == 2
    assert raw.artifact_path("live_ledger.parquet").read_bytes() == original


def test_worker_rejects_snapshot_path_mismatch(isolated_settings):
    settings = isolated_settings
    with pytest.raises(ConfigurationError, match="paths"):
        pipeline.point_config(settings.world_dir, settings.state_dir / "another", 45.0, 39.0, settings.compute_config().to_dict())
