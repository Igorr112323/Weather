import base64
import os
import shutil
import sqlite3
from pathlib import Path

from agrocast.core.jsoncodec import strict_json
from agrocast.state.backup import MAX_BACKUP_BYTES, StateError, file_checksum
from agrocast.store.atomic import write_json
from agrocast.store.results import fingerprint

FORMAT = "agrocast-legacy-v1"


def _safe_value(value):
    return {"agrocast_blob_base64": base64.b64encode(value).decode("ascii")} if isinstance(value, bytes) else value


def _sqlite_records(path, label):
    records = []
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise StateError("SQLite snapshot failed integrity check")
        tables = sorted(row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))
        for table in tables:
            quoted = '"' + table.replace('"', '""') + '"'
            rows = [{key: _safe_value(value) for key, value in dict(row).items()} for row in connection.execute(f"SELECT * FROM {quoted}")]
            rows.sort(key=fingerprint)
            for index, data in enumerate(rows):
                identifier = str(data["id"]) if "id" in data else str(index)
                key = f"{label}:{table}:{identifier}"
                if len(key) > 240:
                    raise StateError("legacy source key is too long")
                kind = {"variety": "crop", "subscriptions": "subscription", "forecasts": "publication"}.get(table, "archive")
                records.append({"key": key, "kind": kind, "data": data, "checksum": fingerprint(data)})
    return records


def _export_records(path, kind):
    data = strict_json(path.read_bytes())
    if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
        raise StateError("JSON export must be an array of objects")
    records = []
    for index, row in enumerate(data):
        identifier = str(row.get("id", index))
        key = f"{kind}.json:{identifier}"
        if len(key) > 240:
            raise StateError("legacy export key is too long")
        records.append({"key": key, "kind": {"fields": "field", "jobs": "job"}[kind], "data": row, "checksum": fingerprint(row)})
    return records


def create_snapshot(source_dir, destination, fields_export=None, jobs_export=None, no_in_memory_jobs=False):
    source = Path(source_dir).resolve(strict=True)
    destination = Path(destination).resolve()
    if not source.is_dir() or destination == source or source in destination.parents or destination in source.parents:
        raise StateError("snapshot must be outside the legacy source directory")
    if jobs_export is None and not no_in_memory_jobs:
        raise StateError("export in-memory jobs or explicitly confirm none exist")
    destination.mkdir(parents=True, mode=0o700, exist_ok=False)
    files = []
    records = []
    databases = sorted(set(source.rglob("registry.sqlite")) | set(source.rglob("crops.db")))
    for database in databases:
        if source not in database.resolve().parents or database.is_symlink():
            raise StateError("legacy database must not escape the source directory")
        label = database.relative_to(source).as_posix()
        target = destination / "sqlite" / label
        target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as original:
            with sqlite3.connect(target) as copy:
                original.backup(copy)
        os.chmod(target, 0o600)
        with target.open("rb") as handle:
            os.fsync(handle.fileno())
        rows = _sqlite_records(target, label)
        records.extend(rows)
        files.append({"path": target.relative_to(destination).as_posix(), "checksum": file_checksum(target), "records": len(rows)})
    for kind, export in (("fields", fields_export), ("jobs", jobs_export)):
        if export is None:
            continue
        export = Path(export).resolve(strict=True)
        if export.stat().st_size > MAX_BACKUP_BYTES:
            raise StateError("JSON export exceeds supported size")
        target = destination / (kind + ".json")
        shutil.copyfile(export, target)
        os.chmod(target, 0o600)
        with target.open("rb") as handle:
            os.fsync(handle.fileno())
        rows = _export_records(target, kind)
        records.extend(rows)
        files.append({"path": target.name, "checksum": file_checksum(target), "records": len(rows)})
    if not files:
        raise StateError("no supported SQLite databases or JSON exports found")
    keys = [record["key"] for record in records]
    if len(keys) != len(set(keys)):
        raise StateError("duplicate legacy record identifiers")
    counts = {kind: sum(record["kind"] == kind for record in records) for kind in sorted({record["kind"] for record in records})}
    manifest = {"format": FORMAT, "files": files, "counts": counts, "records": records, "in_memory_jobs": "exported" if jobs_export else "explicitly_absent"}
    manifest["checksum"] = fingerprint(manifest)
    write_json(destination / "manifest.json", manifest, exclusive=True)
    template = {"reviewed": False, "records": {record["key"]: {"action": "quarantine", "reason": "ownership or mapping is not confirmed"} for record in records}}
    write_json(destination / "mapping.template.json", template, exclusive=True)
    return {"snapshot": str(destination), "counts": counts, "checksum": manifest["checksum"]}


def read_snapshot(directory):
    directory = Path(directory).resolve(strict=True)
    path = directory / "manifest.json"
    if path.stat().st_size > MAX_BACKUP_BYTES:
        raise StateError("snapshot manifest exceeds supported size")
    manifest = strict_json(path.read_bytes())
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise StateError("unsupported legacy snapshot")
    if manifest.get("checksum") != fingerprint({key: value for key, value in manifest.items() if key != "checksum"}):
        raise StateError("legacy snapshot manifest checksum mismatch")
    reconciled = []
    for item in manifest["files"]:
        path = directory / item["path"]
        if not path.is_file() or directory not in path.resolve().parents or path.is_symlink():
            raise StateError("snapshot file is missing or escapes snapshot directory")
        if file_checksum(path) != item["checksum"]:
            raise StateError("legacy snapshot file checksum mismatch")
        if item["path"].startswith("sqlite/"):
            rows = _sqlite_records(path, item["path"][len("sqlite/"):])
        else:
            kind = path.stem
            if kind not in {"fields", "jobs"}:
                raise StateError("unsupported JSON snapshot file")
            rows = _export_records(path, kind)
        if len(rows) != item["records"]:
            raise StateError("legacy snapshot record count mismatch")
        reconciled.extend(rows)
    counts = {kind: sum(record["kind"] == kind for record in reconciled) for kind in sorted({record["kind"] for record in reconciled})}
    if counts != manifest["counts"]:
        raise StateError("legacy snapshot record counts do not match")
    if fingerprint(reconciled) != fingerprint(manifest["records"]):
        raise StateError("legacy snapshot record content mismatch")
    return manifest
