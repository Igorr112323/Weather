import hashlib
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from agrocast.core.jsoncodec import canonical_json, strict_json
from agrocast.store.atomic import fsync_directory, write_json

RELEASE_SCHEMA = "bundle-release-v1"
ACTIVE_SCHEMA = "bundle-active-v1"
HISTORY_SCHEMA = "bundle-history-v1"
RELEASE_MANIFEST = "release.json"
ACTIVE_POINTER = "active.json"
HISTORY_FILE = "history.json"
MAX_RELEASE_FILES = 20000
MAX_HISTORY_ENTRIES = 200


class BundleError(ValueError):
    pass


def _utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def file_sha256(path, digest=None):
    hasher = digest or hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def _list_files(root):
    out = []
    for path in sorted(Path(root).rglob("*")):
        if path.is_symlink():
            raise BundleError(f"symlink in bundle: {path.relative_to(root)}")
        if path.is_file() and path.name != RELEASE_MANIFEST:
            out.append(path.relative_to(Path(root)).as_posix())
    if len(out) > MAX_RELEASE_FILES:
        raise BundleError("too many files in bundle")
    return out


def _load_manifest(release_dir):
    try:
        manifest = strict_json((Path(release_dir) / RELEASE_MANIFEST).read_bytes())
    except FileNotFoundError as exc:
        raise BundleError("release manifest missing") from exc
    except (OSError, ValueError, RecursionError) as exc:
        raise BundleError("release manifest unreadable") from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != RELEASE_SCHEMA:
        raise BundleError("release manifest schema mismatch")
    return manifest


def release_digest(manifest):
    return hashlib.sha256(canonical_json(manifest).encode("utf-8")).hexdigest()


def _content_digest(files):
    return {
        "schema": RELEASE_SCHEMA,
        "files": files,
    }


def build_release(source_dir, releases_root, release_id=None, exclude=tuple()):
    source_dir = Path(source_dir)
    releases_root = Path(releases_root)
    if not source_dir.is_dir():
        raise BundleError("source bundle directory missing")
    releases_root.mkdir(parents=True, exist_ok=True)
    names = [name for name in _list_files(source_dir) if name not in set(exclude)]
    if not names:
        raise BundleError("source bundle is empty")
    staging = releases_root / f".staging-{os.getpid()}-{time.time_ns()}"
    staging.mkdir()
    files = []
    try:
        for name in names:
            source = source_dir / name
            target = staging / name
            target.parent.mkdir(parents=True, exist_ok=True)
            hasher = hashlib.sha256()
            size = 0
            with source.open("rb") as reader, target.open("wb") as writer:
                while True:
                    chunk = reader.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    hasher.update(chunk)
                    writer.write(chunk)
                writer.flush()
                os.fsync(writer.fileno())
            if file_sha256(source) != hasher.hexdigest():
                raise BundleError(f"file changed during build: {name}")
            files.append({"path": name, "bytes": size, "sha256": hasher.hexdigest()})
        for directory in sorted({(staging / f["path"]).parent for f in files}, reverse=True):
            fsync_directory(directory)
        fsync_directory(staging)
        manifest = dict(_content_digest(files))
        manifest["total_bytes"] = sum(f["bytes"] for f in files)
        manifest["created_utc"] = _utc_now()
        digest = release_digest(_content_digest(files))
        resolved_id = release_id or digest[:12]
        if len(resolved_id) > 80 or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_." for ch in resolved_id):
            raise BundleError("invalid release id")
        manifest["id"] = resolved_id
        manifest["digest"] = digest
        write_json(staging / RELEASE_MANIFEST, manifest)
        final = releases_root / resolved_id
        if final.exists():
            existing = _load_manifest(final)
            if existing.get("digest") != digest:
                raise BundleError("release id already used with different content")
            problems = verify_release(final)
            if problems:
                raise BundleError("existing release is corrupt: " + "; ".join(problems))
            return {"release_id": resolved_id, "path": str(final), "reused": True, "files": len(files), "total_bytes": manifest["total_bytes"], "digest": digest}
        os.rename(staging, final)
        fsync_directory(releases_root)
        return {"release_id": resolved_id, "path": str(final), "reused": False, "files": len(files), "total_bytes": manifest["total_bytes"], "digest": digest}
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def verify_release(release_dir, full=True):
    release_dir = Path(release_dir)
    try:
        manifest = _load_manifest(release_dir)
    except BundleError as exc:
        return [str(exc)]
    problems = []
    if manifest.get("digest") != release_digest(_content_digest(manifest.get("files", []))):
        problems.append("manifest digest mismatch")
    listed = {entry["path"]: entry for entry in manifest.get("files", [])}
    if len(listed) != len(manifest.get("files", [])):
        problems.append("duplicate paths in manifest")
    on_disk = set()
    try:
        on_disk = set(_list_files(release_dir))
    except BundleError as exc:
        problems.append(str(exc))
    for name in sorted(on_disk - set(listed)):
        problems.append(f"unexpected file: {name}")
    for name in sorted(set(listed) - on_disk):
        problems.append(f"missing file: {name}")
    if not problems:
        for name, entry in sorted(listed.items()):
            try:
                if (release_dir / name).stat().st_size != entry["bytes"]:
                    problems.append(f"size mismatch: {name}")
            except OSError:
                problems.append(f"unreadable: {name}")
    if full and not problems:
        for name, entry in sorted(listed.items()):
            if file_sha256(release_dir / name) != entry["sha256"]:
                problems.append(f"checksum mismatch: {name}")
    return problems


def _read_json(path):
    try:
        return strict_json(Path(path).read_bytes())
    except (OSError, ValueError, RecursionError):
        return None


def active_release(releases_root, full=False):
    releases_root = Path(releases_root)
    pointer = _read_json(releases_root / ACTIVE_POINTER)
    if not isinstance(pointer, dict) or pointer.get("schema") != ACTIVE_SCHEMA:
        return None
    release_id = pointer.get("release_id")
    if not isinstance(release_id, str) or not release_id:
        return {"release_id": None, "path": None, "problems": ["active pointer invalid"]}
    directory = releases_root / release_id
    if not directory.is_dir():
        return {"release_id": release_id, "path": None, "problems": ["active release missing"]}
    problems = verify_release(directory, full=full)
    if problems:
        return {"release_id": release_id, "path": None, "problems": problems}
    return {"release_id": release_id, "path": str(directory), "problems": []}


def _append_history(releases_root, entry):
    releases_root = Path(releases_root)
    data = _read_json(releases_root / HISTORY_FILE)
    entries = data.get("entries", []) if isinstance(data, dict) else []
    entries.append(entry)
    write_json(releases_root / HISTORY_FILE, {"schema": HISTORY_SCHEMA, "entries": entries[-MAX_HISTORY_ENTRIES:]})


def set_active(releases_root, release_id, action="activate"):
    releases_root = Path(releases_root)
    directory = releases_root / release_id
    if not directory.is_dir():
        raise BundleError("release does not exist")
    manifest = _load_manifest(directory)
    if manifest.get("id") != release_id:
        raise BundleError("release id does not match manifest")
    problems = verify_release(directory, full=True)
    if problems:
        raise BundleError("refusing to activate corrupt release: " + "; ".join(problems[:5]))
    write_json(releases_root / ACTIVE_POINTER, {"schema": ACTIVE_SCHEMA, "release_id": release_id, "activated_utc": _utc_now()})
    _append_history(releases_root, {"release_id": release_id, "utc": _utc_now(), "action": action})
    return {"release_id": release_id, "activated": True}


def rollback(releases_root):
    releases_root = Path(releases_root)
    current = active_release(releases_root)
    if current is None:
        raise BundleError("no active release to roll back from")
    data = _read_json(releases_root / HISTORY_FILE)
    entries = data.get("entries", []) if isinstance(data, dict) else []
    current_id = current["release_id"]
    for entry in reversed(entries[:-1] if entries and entries[-1].get("release_id") == current_id else entries):
        candidate = entry.get("release_id")
        if candidate and candidate != current_id and (releases_root / str(candidate)).is_dir():
            problems = verify_release(releases_root / str(candidate), full=True)
            if problems:
                continue
            return set_active(releases_root, candidate, action="rollback")
    raise BundleError("no earlier valid release in history")


def publish(source_dir, releases_root, activate=True):
    built = build_release(source_dir, releases_root)
    if activate:
        set_active(releases_root, built["release_id"])
    return {**built, "activated": bool(activate)}


def release_status(releases_root):
    releases_root = Path(releases_root)
    if not releases_root.is_dir():
        return {"status": "none", "releases_root": str(releases_root)}
    active = active_release(releases_root)
    if active is None:
        return {"status": "no_pointer", "releases_root": str(releases_root)}
    if active["problems"]:
        return {"status": "corrupt", "release_id": active["release_id"], "problems": active["problems"][:10]}
    manifest = _load_manifest(Path(active["path"]))
    return {
        "status": "ok",
        "release_id": active["release_id"],
        "digest": manifest.get("digest"),
        "created_utc": manifest.get("created_utc"),
        "files": len(manifest.get("files", [])),
        "total_bytes": manifest.get("total_bytes"),
    }
