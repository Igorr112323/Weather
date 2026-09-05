import json
import sqlite3
from pathlib import Path
from uuid import uuid4
import os
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.schema import CreateSchema, DropSchema

from agrocast.crops.db import CropDB
from agrocast.identity.credentials import IdentityError
from agrocast.identity.database import migrate
from agrocast.identity.schema import jobs, legacy_records, metadata, users
from agrocast.identity.service import IdentityService
from agrocast.ingest.registry import Registry
from agrocast.serve.pilot import pilot_points
from agrocast.serve.product import create_app
from agrocast.state.backup import StateError, backup_database, file_checksum, read_backup, restore_database
from agrocast.state.legacy import import_snapshot, target_id
from agrocast.state.snapshot import create_snapshot, read_snapshot
from agrocast.store.atomic import write_json
from agrocast.store.results import fingerprint


@pytest.fixture
def legacy_source(tmp_path):
    source = tmp_path / "legacy"
    source.mkdir()
    point = pilot_points()[0]
    CropDB(source / "crops.db").upsert({"name": "Сохранённый сорт", "breeder": "A", "maturity": "early", "fao": 200, "sow_from": "04-20", "sow_to": "05-15", "area_ha": 12.5, "notes": "Исходное содержимое <img src=x onerror=alert(1)>"})
    registry = Registry(source / "registry.sqlite")
    registry.conn.execute("PRAGMA journal_mode=WAL")
    registry.add_subscription("Подписка", point["lat"], point["lon"], 3, ("t2m", "tp"), mode="seasonal")
    report = {"lat": point["lat"], "lon": point["lon"], "start": "2025-10", "months": [{"value": "original publication"}], "agro": {"comment": "historical and unverified"}}
    registry.save_forecast(point["lat"], point["lon"], 2025, 10, 3, report)
    registry.log_event("ingest", "original technical record")
    fields_json = tmp_path / "fields-export.json"
    jobs_json = tmp_path / "jobs-export.json"
    write_json(fields_json, [{"id": "browser-field", "name": "Поле", "lat": point["lat"], "lon": point["lon"], "area": 12.5, "crop": "corn"}])
    write_json(jobs_json, [{"id": "done-job", "status": "done", "result": report, "params": {"start": "2025-10"}, "log": ["preserved"]}, {"id": "in-flight-job", "status": "running", "result": None, "params": {"start": "2026-10"}, "log": ["interrupted before migration"]}])
    try:
        yield source, fields_json, jobs_json, registry
    finally:
        registry.conn.close()


@pytest.fixture
def migration_case(legacy_source, identity, tmp_path):
    source, fields_json, jobs_json, registry = legacy_source
    snapshot = tmp_path / "snapshot"
    create_snapshot(source, snapshot, fields_json, jobs_json)
    manifest = read_snapshot(snapshot)
    with identity.engine.connect() as connection:
        owner = connection.execute(select(users).where(users.c.username == "operator_a")).mappings().one()
    base = {"action": "import", "owner_id": owner["id"], "organization_id": owner["organization_id"]}
    mapping = {"reviewed": True, "records": {}}
    for record in manifest["records"]:
        entry = dict(base)
        if record["kind"] == "archive":
            entry = {"action": "quarantine", "reason": "technical record has no confirmed user owner"}
        elif record["kind"] == "field":
            entry["point_id"] = pilot_points()[0]["id"]
        elif record["kind"] == "subscription":
            entry.update(field_key="fields.json:browser-field", start_month=10, season_len=3, variety_key="crops.db:variety:1", variety_revision=1)
        elif record["kind"] == "job" and record["data"]["status"] == "running":
            entry["interrupt"] = True
        mapping["records"][record["key"]] = entry
    mapping_path = tmp_path / "mapping.json"
    write_json(mapping_path, mapping)
    return snapshot, mapping_path, manifest, mapping


def counts(engine):
    with engine.connect() as connection:
        return {table.name: connection.execute(select(func.count()).select_from(table)).scalar_one() for table in metadata.sorted_tables}


def test_snapshot_is_a_verified_sqlite_backup_not_a_mutated_original(legacy_source, tmp_path):
    source, fields_json, jobs_json, registry = legacy_source
    before = {path: file_checksum(path) for path in source.glob("*.db")}
    snapshot = tmp_path / "copy"
    summary = create_snapshot(source, snapshot, fields_json, jobs_json)
    manifest = read_snapshot(snapshot)
    assert summary["checksum"] == manifest["checksum"]
    assert sum(manifest["counts"].values()) == 7
    assert before == {path: file_checksum(path) for path in source.glob("*.db")}
    registry.add_subscription("Added after snapshot", 45.0, 39.0, 3, ("t2m",))
    with sqlite3.connect(snapshot / "sqlite/registry.sqlite") as connection:
        assert connection.execute("SELECT count(*) FROM subscriptions").fetchone()[0] == 1
    assert read_snapshot(snapshot)["counts"]["subscription"] == 1
    assert (snapshot / "manifest.json").stat().st_mode & 0o777 == 0o600
    assert (snapshot / "sqlite/registry.sqlite").stat().st_mode & 0o777 == 0o600


def test_snapshot_requires_an_explicit_in_memory_job_decision(legacy_source, tmp_path):
    with pytest.raises(StateError, match="in-memory jobs"):
        create_snapshot(legacy_source[0], tmp_path / "copy")
    create_snapshot(legacy_source[0], tmp_path / "copy", no_in_memory_jobs=True)
    assert read_snapshot(tmp_path / "copy")["in_memory_jobs"] == "explicitly_absent"


def test_legacy_import_preserves_counts_content_and_owner_scope(migration_case, identity, clients, tmp_path):
    snapshot, mapping_path, manifest, mapping = migration_case
    before = backup_database(identity.engine, tmp_path / "before.json")
    result = import_snapshot(identity.engine, snapshot, mapping_path, "pilot-2026")
    assert result["verified"] is True
    assert result["imported"] == 6
    assert result["quarantined"] == 1
    assert result["archived_count"] == 7
    assert result["target_counts"] == {"crops": 1, "fields": 1, "subscriptions": 1, "jobs": 3, "publications": 2}
    client = clients()
    subscription = client.get("/api/subscriptions").json()["subscriptions"][0]
    assert subscription["data"]["start_month"] == 10
    assert subscription["data"]["active"] is False
    assert subscription["data"]["horizon"] == 3
    assert subscription["data"]["variety_revision"] == 1
    crop = client.get("/api/crops").json()["crops"][0]
    assert crop["data"]["area_ha"] == 12.5
    assert crop["data"]["maturity"] == "early"
    assert "onerror" in crop["data"]["notes"]
    with identity.engine.connect() as connection:
        archive = connection.execute(select(legacy_records)).mappings().all()
        for row in archive:
            original = next(record for record in manifest["records"] if record["key"] == row["source_key"])
            assert row["checksum"] == original["checksum"] == fingerprint(row["data"]["source"])
        interrupted = connection.execute(select(jobs).where(jobs.c.id == target_id("pilot-2026", "jobs.json:in-flight-job", "jobs"))).mappings().one()
        assert interrupted["status"] == "failed"
        assert interrupted["data"]["error"] == "legacy_job_interrupted"
        assert interrupted["data"]["log"] == ["interrupted before migration"]
    response = client.get("/api/publications")
    assert response.status_code == 200
    assert response.json()["validation"]["production_ready"] is False
    publication = response.json()["publications"][0]
    assert fingerprint(publication["data"]) == publication["checksum"]
    publication_url = "/api/publications/" + publication["id"]
    assert clients("admin_a").get(publication_url).status_code == 404
    assert clients("operator_b").get(publication_url).status_code == 404
    assert clients("reader_a").get("/api/publications").json()["publications"] == []
    assert client.delete("/api/jobs/" + publication["job_id"]).status_code == 409
    assert client.delete(publication_url).status_code == 403
    assert clients("admin_a").post("/api/publications", json={}).status_code == 403
    assert before["counts"]["subscriptions"] == 0


def test_import_idempotency_does_not_duplicate_records(migration_case, identity):
    snapshot, mapping, _, _ = migration_case
    first = import_snapshot(identity.engine, snapshot, mapping, "stable-import")
    before = counts(identity.engine)
    second = import_snapshot(identity.engine, snapshot, mapping, "stable-import")
    assert first["already_applied"] is False
    assert second["already_applied"] is True
    assert counts(identity.engine) == before


@pytest.mark.parametrize("case", ["missing_owner", "wrong_organization", "missing_record", "extra_record", "not_reviewed", "missing_start", "missing_season_len", "missing_field", "wrong_point", "no_interruption", "bad_variety_revision", "unexpected_mapping_key"])
def test_incomplete_or_ambiguous_mapping_rolls_back_every_record(migration_case, identity, case):
    snapshot, mapping_path, _, mapping = migration_case
    key = "registry.sqlite:subscriptions:1"
    if case == "missing_owner":
        mapping["records"][key].pop("owner_id")
    elif case == "wrong_organization":
        with identity.engine.connect() as connection:
            organization = connection.execute(select(users.c.organization_id).where(users.c.username == "operator_b")).scalar_one()
        mapping["records"][key]["organization_id"] = organization
    elif case == "missing_record":
        mapping["records"].pop(key)
    elif case == "extra_record":
        mapping["records"]["unrelated"] = {"action": "quarantine", "reason": "not in snapshot"}
    elif case == "not_reviewed":
        mapping["reviewed"] = False
    elif case in {"missing_start", "missing_season_len", "missing_field"}:
        mapping["records"][key].pop({"missing_start": "start_month", "missing_season_len": "season_len", "missing_field": "field_key"}[case])
    elif case == "wrong_point":
        mapping["records"]["fields.json:browser-field"]["point_id"] = pilot_points()[1]["id"]
    elif case == "no_interruption":
        mapping["records"]["jobs.json:in-flight-job"].pop("interrupt")
    elif case == "bad_variety_revision":
        mapping["records"][key]["variety_revision"] = 2
    else:
        mapping["records"][key]["extra"] = True
    write_json(mapping_path, mapping)
    before = counts(identity.engine)
    with pytest.raises(StateError):
        import_snapshot(identity.engine, snapshot, mapping_path, "must-rollback")
    assert counts(identity.engine) == before


def test_quarantine_is_not_implicitly_assigned_to_first_user(migration_case, identity):
    snapshot, mapping_path, manifest, _ = migration_case
    mapping = {"reviewed": True, "records": {record["key"]: {"action": "quarantine", "reason": "ownership unknown"} for record in manifest["records"]}}
    write_json(mapping_path, mapping)
    result = import_snapshot(identity.engine, snapshot, mapping_path, "quarantine-only")
    assert result["imported"] == 0 and result["quarantined"] == 7
    assert all(value == 0 for value in result["target_counts"].values())
    with identity.engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(legacy_records)).scalar_one() == 7


@pytest.mark.parametrize("target", ["manifest", "sqlite", "export"])
def test_corrupt_snapshot_cannot_import(migration_case, identity, target):
    snapshot, mapping, _, _ = migration_case
    path = {"manifest": snapshot / "manifest.json", "sqlite": snapshot / "sqlite/registry.sqlite", "export": snapshot / "fields.json"}[target]
    if target == "manifest":
        data = json.loads(path.read_text())
        data["counts"]["field"] = 900
        write_json(path, data)
    else:
        with path.open("ab") as handle:
            handle.write(b"changed")
    before = counts(identity.engine)
    with pytest.raises(StateError, match="checksum"):
        import_snapshot(identity.engine, snapshot, mapping, "corrupt-import")
    assert counts(identity.engine) == before


def test_same_namespace_cannot_be_reused_for_different_ownership(migration_case, identity):
    snapshot, mapping_path, _, mapping = migration_case
    import_snapshot(identity.engine, snapshot, mapping_path, "fixed")
    mapping["records"]["jobs.json:in-flight-job"] = {"action": "quarantine", "reason": "changed decision"}
    write_json(mapping_path, mapping)
    with pytest.raises(StateError, match="already used"):
        import_snapshot(identity.engine, snapshot, mapping_path, "fixed")


def test_database_backup_restore_reconciles_content_and_revokes_sessions(migration_case, identity, tmp_path, account_password, runtime_settings):
    snapshot, mapping, _, _ = migration_case
    import_snapshot(identity.engine, snapshot, mapping, "restore-case")
    token = identity.login("operator_a", account_password).token
    path = tmp_path / "database-backup.json"
    summary = backup_database(identity.engine, path)
    payload = read_backup(path)
    assert "sessions" not in payload["tables"]
    assert token not in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        backup_database(identity.engine, path)
    with pytest.raises(StateError, match="empty"):
        restore_database(identity.engine, path)
    schema = None
    if identity.engine.dialect.name == "postgresql":
        schema = "restore_" + uuid4().hex
        with identity.engine.begin() as connection:
            connection.execute(CreateSchema(schema))
        restored = create_engine(identity.engine.url, hide_parameters=True, connect_args={"options": "-csearch_path=" + schema})
    else:
        restored = create_engine("sqlite:///" + str(tmp_path / "restored.db"))

        @event.listens_for(restored, "connect")
        def foreign_keys(connection, record):
            connection.execute("PRAGMA foreign_keys=ON")

    try:
        migrate(restored)
        result = restore_database(restored, path)
        assert result["restored_counts"] == summary["counts"]
        assert result["sessions_restored"] == 0
        service = IdentityService(restored, runtime_settings.public_origin)
        with pytest.raises(IdentityError):
            service.authenticate(token)
        with TestClient(create_app(service, runtime_settings), base_url=runtime_settings.public_origin) as client:
            login = client.post("/api/auth/login", headers={"Origin": runtime_settings.public_origin}, json={"username": "operator_a", "password": account_password})
            assert login.status_code == 200
            for kind, expected in (("fields", 1), ("crops", 1), ("subscriptions", 1), ("jobs", 3), ("publications", 2)):
                response = client.get("/api/" + kind)
                assert response.status_code == 200
                assert len(response.json()[kind]) == expected
    finally:
        restored.dispose()
        if schema is not None:
            with identity.engine.begin() as connection:
                connection.execute(DropSchema(schema, cascade=True))


def test_database_corruption_is_rejected_before_target_is_touched(identity, identity_engine, tmp_path):
    path = tmp_path / "backup.json"
    backup_database(identity_engine, path)
    payload = json.loads(path.read_text())
    payload["tables"]["users"][0]["role"] = "admin"
    payload["tables"]["users"][0]["username"] = "tampered"
    write_json(path, payload)
    before = counts(identity_engine)
    with pytest.raises(StateError, match="checksum"):
        restore_database(identity_engine, path)
    assert counts(identity_engine) == before


def test_recreated_api_process_reads_persistent_resources_and_same_settings(migration_case, identity, account_password, tmp_path):
    if identity.engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL process-recreation integration test")
    snapshot, mapping, _, _ = migration_case
    import_snapshot(identity.engine, snapshot, mapping, "process-restart")
    token_path = tmp_path / "session-token"
    token_path.write_text(identity.login("operator_a", account_password).token)
    token_path.chmod(0o600)
    with identity.engine.connect() as connection:
        schema = connection.execute(text("SELECT current_schema()")).scalar_one()
    url = identity.engine.url.update_query_dict({"options": "-csearch_path=" + schema})
    secret = tmp_path / "database-url"
    secret.write_text(url.render_as_string(hide_password=False))
    secret.chmod(0o600)
    environment = {key: value for key, value in os.environ.items() if not key.startswith("AGROCAST_")}
    environment.update(AGROCAST_DATABASE_URL_FILE=str(secret), AGROCAST_PUBLIC_ORIGIN="https://testserver", AGROCAST_STATE_DIR=str(tmp_path / "process-state"))
    program = r'''import json,sys
from pathlib import Path
from fastapi.testclient import TestClient
from agrocast.serve.product import create_app
from agrocast.store.results import fingerprint
app = create_app()
assert app.state.identity is None
with TestClient(app, base_url="https://testserver", headers={"Cookie": "__Host-agrocast_session=" + Path(sys.argv[1]).read_text()}) as client:
    output = {}
    for kind in ("fields", "crops", "subscriptions", "jobs", "publications"):
        response = client.get("/api/" + kind)
        assert response.status_code == 200
        rows = response.json()[kind]
        output[kind] = {"count": len(rows), "checksum": fingerprint(rows)}
    output["settings"] = app.state.settings.fingerprint()
    print(json.dumps(output, sort_keys=True))
assert app.state.identity is None
'''
    results = []
    for _ in range(2):
        child = subprocess.run([sys.executable, "-c", program, str(token_path)], cwd=Path(__file__).resolve().parents[1], env=environment, capture_output=True, text=True, timeout=30)
        assert child.returncode == 0, child.stderr
        results.append(json.loads(child.stdout))
    assert results[0] == results[1]
    assert {kind: value["count"] for kind, value in results[0].items() if kind != "settings"} == {"fields": 1, "crops": 1, "subscriptions": 1, "jobs": 3, "publications": 2}
