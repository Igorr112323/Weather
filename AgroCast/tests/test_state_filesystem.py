import json
from dataclasses import replace

import pytest

from agrocast.core.settings import RuntimeSettings
from agrocast.state.backup import StateError
from agrocast.state.filesystem import backup_runtime, read_runtime_backup, restore_runtime
from agrocast.store.atomic import write_json
from agrocast.store.results import fingerprint


@pytest.fixture
def runtime(tmp_path):
    settings = RuntimeSettings(world_dir=tmp_path / "world", state_dir=tmp_path / "state")
    settings.prepare_state()
    write_json(settings.state_dir / "offline-jobs/job.json", {"id": "unassigned", "status": "running"})
    write_json(settings.state_dir / "results-v1/result.json", {"result": [1, 2, 3]})
    (settings.state_dir / "compute/data.bin").write_bytes(b"\x00\xffpayload")
    return settings


def test_runtime_backup_restore_is_private_verified_and_does_not_resume_jobs(runtime, tmp_path):
    backup = tmp_path / "filesystem-backup"
    result = backup_runtime(runtime, backup)
    assert result["files"] == 3
    manifest = read_runtime_backup(backup)
    assert manifest["checksum"] == result["checksum"]
    target = replace(runtime, state_dir=tmp_path / "recreated-state")
    restored = restore_runtime(target, backup)
    assert restored["files"] == 3
    assert json.loads((target.state_dir / "offline-jobs/job.json").read_text())["status"] == "running"
    assert (target.state_dir / "compute/data.bin").read_bytes() == b"\x00\xffpayload"
    assert (target.state_dir / "offline-jobs/job.json").stat().st_mode & 0o777 == 0o600
    assert sorted(path.relative_to(runtime.state_dir) for path in runtime.state_dir.rglob("*")) == sorted(path.relative_to(target.state_dir) for path in target.state_dir.rglob("*"))
    with pytest.raises(StateError, match="empty"):
        restore_runtime(target, backup)
    with pytest.raises(FileExistsError):
        backup_runtime(runtime, backup)


@pytest.mark.parametrize("case", ["content", "manifest", "missing", "extra", "symlink", "path_traversal", "duplicate"])
def test_runtime_backup_rejects_corruption_without_partial_restore(runtime, tmp_path, case):
    backup = tmp_path / "backup"
    backup_runtime(runtime, backup)
    file = backup / "files/compute/data.bin"
    if case == "content":
        file.write_bytes(b"tampered")
    elif case == "missing":
        file.unlink()
    elif case == "extra":
        (backup / "files/extra").write_bytes(b"unexpected")
    elif case == "symlink":
        file.unlink()
        file.symlink_to(runtime.state_dir / "compute/data.bin")
    else:
        path = backup / "manifest.json"
        manifest = json.loads(path.read_text())
        if case == "manifest":
            manifest["count"] = 400
        elif case == "duplicate":
            manifest["files"].append(manifest["files"][0])
            manifest["checksum"] = fingerprint({key: value for key, value in manifest.items() if key != "checksum"})
        else:
            manifest["files"][0]["path"] = "../outside"
            manifest["checksum"] = fingerprint({key: value for key, value in manifest.items() if key != "checksum"})
        write_json(path, manifest)
    target = replace(runtime, state_dir=tmp_path / "new-state")
    with pytest.raises(StateError):
        restore_runtime(target, backup)
    assert not target.state_dir.exists()


def test_backup_cannot_follow_source_symlinks_or_contain_itself(runtime, tmp_path):
    with pytest.raises(StateError, match="outside"):
        backup_runtime(runtime, runtime.state_dir / "backups/self")
    (runtime.state_dir / "escape").symlink_to(tmp_path)
    with pytest.raises(StateError, match="symlinks"):
        backup_runtime(runtime, tmp_path / "backup")


def test_partial_copy_failure_never_replaces_previous_state(runtime, tmp_path, monkeypatch):
    import agrocast.state.filesystem as filesystem

    backup = tmp_path / "backup"
    backup_runtime(runtime, backup)
    target = replace(runtime, state_dir=tmp_path / "new-state")

    def failure(*args):
        raise OSError("disk unavailable")

    monkeypatch.setattr(filesystem, "_copy_file", failure)
    with pytest.raises(OSError):
        restore_runtime(target, backup)
    assert not target.state_dir.exists()
    assert not list(tmp_path.glob(".agrocast-restore-*"))
    assert json.loads((runtime.state_dir / "offline-jobs/job.json").read_text())["status"] == "running"
