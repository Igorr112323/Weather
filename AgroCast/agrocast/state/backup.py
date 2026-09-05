import hashlib
import time
from pathlib import Path

from sqlalchemy import delete, func, insert, select

from agrocast.core.jsoncodec import strict_json, canonical_json
from agrocast.identity.database import check_schema
from agrocast.identity.schema import REVISION, metadata
from agrocast.store.atomic import write_json
from agrocast.store.results import fingerprint

MAX_BACKUP_BYTES = 128 * 1024 * 1024
EXCLUDED = {"sessions", "login_limits"}


class StateError(ValueError):
    pass


def _rows(connection, table):
    query = select(table).order_by(*table.primary_key.columns)
    return [dict(row) for row in connection.execute(query).mappings()]


def backup_database(engine, path):
    check_schema(engine)
    with engine.connect() as connection:
        if engine.dialect.name == "postgresql":
            connection = connection.execution_options(isolation_level="REPEATABLE READ")
        with connection.begin():
            tables = {table.name: _rows(connection, table) for table in metadata.sorted_tables if table.name not in EXCLUDED}
    payload = {"format": "agrocast-db-v1", "revision": REVISION, "created_at": int(time.time()), "excluded": sorted(EXCLUDED), "tables": tables}
    payload["checksums"] = {name: fingerprint(rows) for name, rows in tables.items()}
    payload["counts"] = {name: len(rows) for name, rows in tables.items()}
    payload["checksum"] = fingerprint(payload)
    if len(canonical_json(payload).encode("utf-8")) > MAX_BACKUP_BYTES:
        raise StateError("database exceeds supported logical backup size; use a PostgreSQL-native backup")
    write_json(path, payload, exclusive=True)
    return {"path": str(path), "counts": payload["counts"], "checksum": payload["checksum"]}


def read_backup(path):
    with Path(path).open("rb") as handle:
        data = handle.read(MAX_BACKUP_BYTES + 1)
    if len(data) > MAX_BACKUP_BYTES:
        raise StateError("backup exceeds supported size")
    payload = strict_json(data)
    if not isinstance(payload, dict) or payload.get("format") != "agrocast-db-v1" or payload.get("revision") != REVISION:
        raise StateError("backup format or schema does not match")
    checksum = payload.get("checksum")
    if fingerprint({key: value for key, value in payload.items() if key != "checksum"}) != checksum:
        raise StateError("backup checksum mismatch")
    tables = payload.get("tables", {})
    expected = {table.name for table in metadata.sorted_tables if table.name not in EXCLUDED}
    if set(tables) != expected or payload.get("excluded") != sorted(EXCLUDED):
        raise StateError("backup table set does not match")
    for name, rows in tables.items():
        if not isinstance(rows, list) or payload["counts"].get(name) != len(rows) or payload["checksums"].get(name) != fingerprint(rows):
            raise StateError("backup table validation failed")
    return payload


def restore_database(engine, path):
    payload = read_backup(path)
    check_schema(engine)
    with engine.begin() as connection:
        for table in metadata.sorted_tables:
            count = connection.execute(select(func.count()).select_from(table)).scalar_one()
            if table.name == "login_limits":
                if count > 1:
                    raise StateError("restore target is not empty")
            elif count:
                raise StateError("restore requires an empty migrated database")
        for table in metadata.sorted_tables:
            if table.name in EXCLUDED:
                continue
            rows = payload["tables"][table.name]
            if rows:
                connection.execute(insert(table), rows)
            restored = _rows(connection, table)
            if len(restored) != payload["counts"][table.name] or fingerprint(restored) != payload["checksums"][table.name]:
                raise StateError("restored content does not match backup")
        from agrocast.identity.schema import sessions, login_limits

        connection.execute(delete(sessions))
        connection.execute(delete(login_limits))
        connection.execute(insert(login_limits).values(key="global", window=0, attempts=0))
    return {"restored_counts": payload["counts"], "checksum": payload["checksum"], "sessions_restored": 0}


def file_checksum(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
