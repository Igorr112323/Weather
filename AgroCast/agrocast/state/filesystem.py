import os
import shutil
import tempfile
from pathlib import Path

from agrocast.core.jsoncodec import strict_json
from agrocast.state.backup import MAX_BACKUP_BYTES, StateError, file_checksum
from agrocast.store.atomic import write_json
from agrocast.store.results import fingerprint


def _copy_file(source, target):
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with source.open("rb") as original, target.open("xb") as copy:
        os.chmod(target, 0o600)
        shutil.copyfileobj(original, copy)
        copy.flush()
        os.fsync(copy.fileno())


def _sync_directories(root):
    directories = [root, *[path for path in root.rglob("*") if path.is_dir()]]
    for directory in reversed(directories):
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def backup_runtime(settings, directory):
    source = settings.state_dir
    directory = settings.writable_path(directory)
    if source == directory or source in directory.parents or directory in source.parents:
        raise StateError("runtime backup must be outside the state directory")
    if not source.is_dir():
        raise StateError("runtime state directory does not exist")
    paths = sorted(source.rglob("*"))
    if any(path.is_symlink() or not (path.is_file() or path.is_dir()) for path in paths):
        raise StateError("runtime backup does not accept symlinks or special files")
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    root = directory / "files"
    root.mkdir(mode=0o700)
    files = []
    directories = []
    for path in paths:
        relative = path.relative_to(source)
        if path.is_dir():
            (root / relative).mkdir(parents=True, exist_ok=True, mode=0o700)
            directories.append(relative.as_posix())
            continue
        checksum = file_checksum(path)
        target = root / relative
        _copy_file(path, target)
        if file_checksum(target) != checksum or file_checksum(path) != checksum:
            raise StateError("runtime file changed during backup; stop all writers")
        files.append({"path": relative.as_posix(), "bytes": target.stat().st_size, "checksum": checksum})
    if [path.relative_to(source).as_posix() for path in sorted(source.rglob("*"))] != [path.relative_to(source).as_posix() for path in paths]:
        raise StateError("runtime directory changed during backup; stop all writers")
    manifest = {"format": "agrocast-runtime-v1", "directories": directories, "files": files, "count": len(files)}
    manifest["checksum"] = fingerprint(manifest)
    write_json(directory / "manifest.json", manifest, exclusive=True)
    _sync_directories(directory)
    return {"runtime_backup": str(directory), "files": len(files), "checksum": manifest["checksum"]}


def _relative(value):
    if not isinstance(value, str):
        raise StateError("runtime manifest contains an invalid path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or not path.parts or path.as_posix() != value:
        raise StateError("runtime manifest path must be a canonical relative path")
    return path


def read_runtime_backup(directory):
    directory = Path(directory).resolve(strict=True)
    path = directory / "manifest.json"
    if path.stat().st_size > MAX_BACKUP_BYTES:
        raise StateError("runtime manifest exceeds supported size")
    manifest = strict_json(path.read_bytes())
    if not isinstance(manifest, dict) or manifest.get("format") != "agrocast-runtime-v1" or manifest.get("checksum") != fingerprint({key: value for key, value in manifest.items() if key != "checksum"}):
        raise StateError("runtime manifest checksum or format is invalid")
    root = directory / "files"
    if not root.is_dir() or root.is_symlink():
        raise StateError("runtime backup files directory is missing or invalid")
    listed = set()
    for item in manifest["files"]:
        relative = _relative(item["path"])
        if item["path"] in listed:
            raise StateError("duplicate runtime backup path")
        listed.add(item["path"])
        source = root / relative
        if not source.is_file() or source.is_symlink() or root not in source.resolve().parents or source.stat().st_size != item["bytes"] or file_checksum(source) != item["checksum"]:
            raise StateError("runtime backup content checksum does not match")
    for name in manifest["directories"]:
        relative = _relative(name)
        if name in listed:
            raise StateError("duplicate runtime backup path")
        listed.add(name)
        if not (root / relative).is_dir() or (root / relative).is_symlink():
            raise StateError("runtime backup directory does not match")
    actual = {path.relative_to(root).as_posix() for path in root.rglob("*")}
    if actual != listed or len(manifest["files"]) != manifest["count"]:
        raise StateError("runtime backup counts do not match")
    return manifest


def restore_runtime(settings, directory):
    source = Path(directory).resolve(strict=True)
    target = settings.writable_path(settings.state_dir)
    if target == source or source in target.parents or target in source.parents:
        raise StateError("runtime restore source and target must not overlap")
    manifest = read_runtime_backup(source)
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise StateError("runtime restore requires an empty state directory")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix=".agrocast-restore-", dir=target.parent) as stage_name:
        stage = Path(stage_name) / "state"
        stage.mkdir(mode=0o700)
        for name in manifest["directories"]:
            (stage / _relative(name)).mkdir(parents=True, exist_ok=True, mode=0o700)
        for item in manifest["files"]:
            path = _relative(item["path"])
            _copy_file(source / "files" / path, stage / path)
            if file_checksum(stage / path) != item["checksum"]:
                raise StateError("runtime restore content does not match")
        _sync_directories(stage)
        os.rename(stage, target)
        descriptor = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return {"runtime_restored": str(target), "files": manifest["count"], "checksum": manifest["checksum"]}
