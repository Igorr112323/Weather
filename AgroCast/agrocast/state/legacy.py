import re
import time
from datetime import datetime
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import func, insert, select, text

from agrocast.core.jsoncodec import strict_json
from agrocast.identity.database import check_schema
from agrocast.identity.schema import crops, fields, jobs, legacy_records, migration_runs, publications, subscriptions, users
from agrocast.serve.account_models import CropBody, FieldBody, SubscriptionBody
from agrocast.serve.pilot import pilot_points
from agrocast.serve.responses import CropRecord, FieldRecord, JobRecord, PublicationRecord, SubscriptionRecord
from agrocast.state.backup import StateError
from agrocast.state.snapshot import read_snapshot
from agrocast.store.results import fingerprint

TABLES = {table.name: table for table in (crops, fields, jobs, publications, subscriptions)}
MODELS = {"crops": CropRecord, "fields": FieldRecord, "jobs": JobRecord, "publications": PublicationRecord, "subscriptions": SubscriptionRecord}


def target_id(namespace, key, table):
    return str(uuid5(NAMESPACE_URL, f"agrocast:legacy:{namespace}:{key}:{table}"))


def _epoch(data, now):
    value = data.get("created_at", data.get("updated_at"))
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    if isinstance(value, str):
        try:
            date = datetime.fromisoformat(value)
            if date.tzinfo is not None:
                return int(date.timestamp())
        except ValueError:
            pass
    return now


def _mapping(path, manifest):
    mapping = strict_json(Path(path).read_bytes())
    if not isinstance(mapping, dict) or set(mapping) != {"reviewed", "records"} or mapping["reviewed"] is not True:
        raise StateError("ownership mapping must be explicitly reviewed")
    if not isinstance(mapping["records"], dict) or set(mapping["records"]) != {record["key"] for record in manifest["records"]}:
        raise StateError("mapping must cover every source record exactly once")
    for value in mapping["records"].values():
        if not isinstance(value, dict) or value.get("action") not in {"import", "quarantine"}:
            raise StateError("every record must be imported or quarantined")
        if value["action"] == "quarantine":
            if set(value) != {"action", "reason"} or not isinstance(value.get("reason"), str) or not value["reason"].strip():
                raise StateError("quarantine requires a reason and must not assign ownership")
        else:
            permitted = {"action", "owner_id", "organization_id", "point_id", "field_key", "field_id", "start_month", "season_len", "interrupt", "variety_key", "variety_id", "variety_revision"}
            if set(value) - permitted:
                raise StateError("mapping contains unknown properties")
            try:
                UUID(value["owner_id"])
                UUID(value["organization_id"])
            except (ValueError, KeyError, TypeError, AttributeError):
                raise StateError("import requires an explicit owner and organization") from None
    return mapping


def _field_data(data, mapping):
    values = {"name": data.get("name"), "area_ha": data.get("area_ha", data.get("area")), "point_id": mapping.get("point_id", data.get("point_id"))}
    model = FieldBody.model_validate(values)
    if "lat" in data or "lon" in data:
        point = next(point for point in pilot_points() if point["id"] == model.point_id)
        if data.get("lat") != point["lat"] or data.get("lon") != point["lon"]:
            raise StateError("legacy field coordinates must match the explicitly mapped pilot point")
    if data.get("crop", "maize") not in {"corn", "maize"}:
        raise StateError("legacy field crop is outside the closed pilot")
    return model.model_dump(mode="json")


def _owned_reference(connection, table, resource_id, base):
    row = connection.execute(select(table).where(table.c.id == resource_id, table.c.owner_id == base["owner_id"], table.c.organization_id == base["organization_id"])).mappings().one_or_none()
    if row is None:
        raise StateError("mapped reference must exist for the same owner and organization")
    return row


def _targets(connection, namespace, record, mapping, now):
    key, kind, data = record["key"], record["kind"], record["data"]
    if connection.execute(select(users.c.id).where(users.c.id == mapping["owner_id"], users.c.organization_id == mapping["organization_id"])).scalar_one_or_none() is None:
        raise StateError("mapped owner does not belong to the organization")
    timestamp = _epoch(data, now)
    base = {"owner_id": mapping["owner_id"], "organization_id": mapping["organization_id"], "created_at": timestamp, "updated_at": timestamp}
    provenance = {"namespace": namespace, "source_key": key, "source_checksum": record["checksum"], "scientifically_verified": False}
    if kind == "field":
        return [(fields, {**base, "id": target_id(namespace, key, "fields"), "data": _field_data(data, mapping)})]
    if kind == "crop":
        body = CropBody.model_validate({key: value for key, value in data.items() if key not in {"id", "updated_at", "created_at"}})
        return [(crops, {**base, "id": target_id(namespace, key, "crops"), "revision": 1, "data": body.model_dump(mode="json")})]
    if kind == "subscription":
        if "start_month" not in mapping or "season_len" not in mapping or ("field_key" in mapping) == ("field_id" in mapping):
            raise StateError("legacy subscription requires explicit start_month, season_len and one field mapping")
        field_id = target_id(namespace, mapping["field_key"], "fields") if "field_key" in mapping else str(UUID(mapping["field_id"]))
        field = _owned_reference(connection, fields, field_id, base)
        if "lat" in data or "lon" in data:
            point = next(point for point in pilot_points() if point["id"] == field["data"]["point_id"])
            if data.get("lat") != point["lat"] or data.get("lon") != point["lon"]:
                raise StateError("subscription coordinates do not match the mapped field")
        values = {"name": data.get("name"), "field_id": field_id, "start_month": mapping["start_month"], "season_len": mapping["season_len"], "horizon": data["horizon"], "mode": data["mode"], "variables": strict_json(data["variables"]) if isinstance(data["variables"], str) else data["variables"]}
        if "variety_key" in mapping or "variety_id" in mapping:
            if ("variety_key" in mapping) == ("variety_id" in mapping):
                raise StateError("subscription requires exactly one variety mapping")
            variety_id = target_id(namespace, mapping["variety_key"], "crops") if "variety_key" in mapping else str(UUID(mapping["variety_id"]))
            crop = connection.execute(select(crops).where(crops.c.id == variety_id, crops.c.organization_id == base["organization_id"])).mappings().one_or_none()
            if crop is None or mapping.get("variety_revision") != crop["revision"]:
                raise StateError("subscription variety revision or organization does not match")
            values.update(variety_id=variety_id, variety_revision=mapping["variety_revision"])
        body = SubscriptionBody.model_validate(values)
        result = body.model_dump(mode="json")
        result["active"] = False
        return [(subscriptions, {**base, "id": target_id(namespace, key, "subscriptions"), "field_id": field_id, "data": result})]
    if kind not in {"publication", "job"}:
        raise StateError("technical legacy records must remain quarantined")
    report = strict_json(data["payload"]) if kind == "publication" else data.get("result", data.get("report"))
    status = "succeeded" if kind == "publication" else {"done": "succeeded", "error": "failed", "pending": "queued"}.get(data.get("status"), data.get("status"))
    if status not in {"queued", "running", "succeeded", "failed", "cancelled"}:
        raise StateError("legacy job has an unknown status")
    if status in {"queued", "running"}:
        if mapping.get("interrupt") is not True:
            raise StateError("in-flight jobs require explicit interrupt=true; they cannot be resumed")
        status = "failed"
        provenance["interrupted_status"] = data["status"]
    if report is not None and not isinstance(report, dict):
        raise StateError("legacy report must be a JSON object")
    if status == "succeeded" and report is None:
        raise StateError("completed job must include its report")
    job_id = target_id(namespace, key, "jobs")
    job_data = {"params": data.get("params", {}), "report": report, "error": "legacy_job_interrupted" if "interrupted_status" in provenance else data.get("error"), "log": data.get("log", []), "legacy": provenance}
    targets = [(jobs, {**base, "id": job_id, "status": status, "data": job_data})]
    if status == "succeeded":
        publication_data = {"report": report, "legacy": provenance}
        targets.append((publications, {**base, "id": target_id(namespace, key, "publications"), "job_id": job_id, "checksum": fingerprint(publication_data), "data": publication_data}))
    return targets


def import_snapshot(engine, directory, mapping_path, namespace):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", namespace):
        raise StateError("migration namespace is invalid")
    manifest = read_snapshot(directory)
    mapping = _mapping(mapping_path, manifest)
    mapping_checksum = fingerprint(mapping)
    check_schema(engine)
    now = int(time.time())
    run_id = target_id(namespace, "migration", "migration_runs")
    with engine.begin() as connection:
        if engine.dialect.name == "postgresql":
            connection.execute(text("SELECT pg_advisory_xact_lock(482076012)"))
        previous = connection.execute(select(migration_runs).where(migration_runs.c.namespace == namespace)).mappings().one_or_none()
        if previous is not None:
            if previous["source_checksum"] != manifest["checksum"] or previous["mapping_checksum"] != mapping_checksum:
                raise StateError("migration namespace was already used with different content or mapping")
            return {**previous["report"], "already_applied": True}
        before = {name: connection.execute(select(func.count()).select_from(table)).scalar_one() for name, table in TABLES.items()}
        report = {"namespace": namespace, "source_checksum": manifest["checksum"], "mapping_checksum": mapping_checksum, "source_counts": manifest["counts"], "imported": 0, "quarantined": 0, "target_counts": {name: 0 for name in TABLES}, "verified": False, "already_applied": False}
        connection.execute(insert(migration_runs).values(id=run_id, namespace=namespace, source_checksum=manifest["checksum"], mapping_checksum=mapping_checksum, report=report, created_at=now))
        priority = {"crop": 0, "field": 1, "subscription": 2, "job": 3, "publication": 4, "archive": 5}
        expected_archive = []
        for record in sorted(manifest["records"], key=lambda item: (priority[item["kind"]], item["key"])):
            entry = mapping["records"][record["key"]]
            targets = []
            disposition = "quarantined"
            if entry["action"] == "import":
                try:
                    materialized = _targets(connection, namespace, record, entry, now)
                except StateError:
                    raise
                except (ValueError, TypeError, KeyError, StopIteration):
                    raise StateError("legacy record does not meet the target contract; correct mapping or quarantine it") from None
                for table, row in materialized:
                    try:
                        MODELS[table.name].model_validate(row)
                    except ValueError:
                        raise StateError("mapped record does not meet the published HTTP contract") from None
                    connection.execute(insert(table).values(**row))
                    written = dict(connection.execute(select(table).where(table.c.id == row["id"])).mappings().one())
                    if fingerprint(written) != fingerprint(row):
                        raise StateError("imported target content differs from the mapped record")
                    report["target_counts"][table.name] += 1
                    targets.append({"table": table.name, "id": row["id"], "checksum": fingerprint(row)})
                disposition = "imported"
            report[disposition] += 1
            archive = {"id": target_id(namespace, record["key"], "legacy_records"), "migration_id": run_id, "source_key": record["key"], "kind": record["kind"], "disposition": disposition, "data": {"source": record["data"], "mapping": entry}, "checksum": record["checksum"], "targets": targets}
            connection.execute(insert(legacy_records).values(**archive))
            expected_archive.append(archive)
        archived = [dict(row) for row in connection.execute(select(legacy_records).where(legacy_records.c.migration_id == run_id).order_by(legacy_records.c.source_key)).mappings()]
        if fingerprint(archived) != fingerprint(sorted(expected_archive, key=lambda item: item["source_key"])):
            raise StateError("archived source counts or content do not match")
        for name, table in TABLES.items():
            count = connection.execute(select(func.count()).select_from(table)).scalar_one()
            if count - before[name] != report["target_counts"][name]:
                raise StateError("target record count reconciliation failed")
        if report["imported"] + report["quarantined"] != len(manifest["records"]):
            raise StateError("source record count reconciliation failed")
        report.update(verified=True, archived_count=len(archived), archive_checksum=fingerprint(archived))
        connection.execute(migration_runs.update().where(migration_runs.c.id == run_id).values(report=report))
    return report
