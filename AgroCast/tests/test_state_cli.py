import json
from types import SimpleNamespace

import pytest

from agrocast.core.settings import RuntimeSettings
from agrocast.crops.db import CropDB
from agrocast.state import cli
from agrocast.store.atomic import write_json


@pytest.fixture
def cli_environment(tmp_path, monkeypatch, identity):
    monkeypatch.setenv("AGROCAST_STATE_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("AGROCAST_PUBLIC_ORIGIN", "https://testserver")
    monkeypatch.setattr(RuntimeSettings, "identity_settings", lambda self: SimpleNamespace(engine=lambda: identity.engine))
    return tmp_path


def test_cli_snapshot_import_backup_are_explicit_and_idempotent(cli_environment, capsys):
    root = cli_environment
    source = root / "legacy"
    CropDB(source / "crops.db").upsert({"name": "unassigned"})
    snapshot = root / "snapshot"
    cli.main(["snapshot-legacy", "--source", str(source), "--output", str(snapshot), "--no-in-memory-jobs"])
    assert json.loads(capsys.readouterr().out)["counts"] == {"crop": 1}
    cli.main(["verify-snapshot", "--snapshot", str(snapshot)])
    assert json.loads(capsys.readouterr().out)["verified"]
    mapping = json.loads((snapshot / "mapping.template.json").read_text())
    mapping["reviewed"] = True
    write_json(root / "mapping.json", mapping)
    arguments = ["import-legacy", "--snapshot", str(snapshot), "--mapping", str(root / "mapping.json"), "--namespace", "cli-rehearsal", "--maintenance-confirmed"]
    for index in range(2):
        cli.main([*arguments, "--backup", str(root / f"before-{index}.json"), "--report", str(root / f"report-{index}.json")])
        report = json.loads(capsys.readouterr().out)
        assert report["verified"] and report["quarantined"] == 1
        assert report["already_applied"] is bool(index)
        assert (root / f"before-{index}.json").exists()
        assert json.loads((root / f"report-{index}.json").read_text()) == report
    cli.main(["backup", "--output", str(root / "after.json")])
    assert json.loads(capsys.readouterr().out)["counts"]["legacy_records"] == 1


@pytest.mark.parametrize("command,extra", [
    ("import-legacy", ["--snapshot", "absent", "--mapping", "absent", "--namespace", "test", "--backup", "before", "--report", "report"]),
    ("restore", ["--backup", "absent"]),
    ("backup-runtime", ["--output", "absent"]),
    ("restore-runtime", ["--backup", "absent"]),
])
def test_cli_requires_maintenance_confirmation(cli_environment, monkeypatch, command, extra):
    monkeypatch.chdir(cli_environment)
    with pytest.raises(SystemExit, match="confirmed"):
        cli.main([command, *extra])
    assert not (cli_environment / "before").exists()
    assert not (cli_environment / "report").exists()


def test_cli_round_trips_filesystem_state_and_exports_offline_jobs(cli_environment, monkeypatch, capsys):
    root = cli_environment
    settings = RuntimeSettings.from_environment()
    settings.prepare_state()
    write_json(settings.state_dir / "offline-jobs/job.json", {"id": "job", "status": "running"})
    cli.main(["export-offline-jobs", "--output", str(root / "jobs.json")])
    assert json.loads(capsys.readouterr().out)["jobs"] == 1
    assert json.loads((root / "jobs.json").read_text()) == [{"id": "job", "status": "running"}]
    cli.main(["backup-runtime", "--output", str(root / "runtime-backup"), "--maintenance-confirmed"])
    assert json.loads(capsys.readouterr().out)["files"] == 1
    monkeypatch.setenv("AGROCAST_STATE_DIR", str(root / "restored-state"))
    cli.main(["restore-runtime", "--backup", str(root / "runtime-backup"), "--empty-target-confirmed"])
    assert json.loads(capsys.readouterr().out)["files"] == 1
    assert json.loads((root / "restored-state/offline-jobs/job.json").read_text())["status"] == "running"


def test_cli_error_does_not_print_credentials_or_modify_bundle(cli_environment, capsys, monkeypatch):
    root = cli_environment
    monkeypatch.setenv("AGROCAST_WORLD_DIR", str(root / "world"))
    with pytest.raises(SystemExit, match="read-only"):
        cli.main(["backup", "--output", str(root / "world/forbidden.json")])
    assert not (root / "world").exists()

    def fail(*args, **kwargs):
        raise OSError("postgresql://user:private-password@server")

    monkeypatch.setattr(cli, "backup_database", fail)
    with pytest.raises(SystemExit) as failure:
        cli.main(["backup", "--output", str(root / "backup.json")])
    assert "private-password" not in str(failure.value)
    assert "private-password" not in capsys.readouterr().out


def test_atomic_state_failure_does_not_destroy_previous_file(tmp_path, monkeypatch):
    import agrocast.store.atomic as atomic

    path = tmp_path / "value.json"
    atomic.write_json(path, {"previous": True})

    def unavailable(*args):
        raise OSError("replace failed")

    monkeypatch.setattr(atomic.os, "replace", unavailable)
    with pytest.raises(OSError):
        atomic.write_json(path, {"next": True})
    assert json.loads(path.read_text()) == {"previous": True}
    assert not list(tmp_path.glob(".*.tmp"))


def test_database_factory_startup_error_is_sanitized_and_engine_is_disposed(runtime_settings, monkeypatch):
    from fastapi.testclient import TestClient
    from agrocast.core.settings import ConfigurationError
    from agrocast.serve import runtime
    from agrocast.serve.product import create_app

    disposed = []
    engine = SimpleNamespace(dispose=lambda: disposed.append(True))
    options = SimpleNamespace(engine=lambda: engine, public_origin=runtime_settings.public_origin, session_seconds=28800)
    monkeypatch.setattr(RuntimeSettings, "identity_settings", lambda self: options)

    def fail(*args):
        raise OSError("postgresql://user:private-password@server")

    monkeypatch.setattr(runtime, "IdentityService", fail)
    with pytest.raises(ConfigurationError) as error:
        with TestClient(create_app(settings=runtime_settings)):
            pytest.fail("startup must not succeed")
    assert "private-password" not in str(error.value)
    assert disposed == [True]
    assert not runtime_settings.state_dir.exists()
