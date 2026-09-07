import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, insert, select

from agrocast.core.settings import RuntimeSettings
from agrocast.identity.schema import jobs, users
from agrocast.serve.product import create_app

ORIGIN = "https://testserver"
VALID_POINT = {"lat": 46.25, "lon": 38.25, "point_id": "P01", "start": "2026-10", "horizon": 3, "mode": "seasonal", "season_len": 3}
DIGESTS = {"data_release": "a" * 64, "model_release": "b" * 64, "application_release": "c" * 64}


def queue_settings(tmp_path, **overrides):
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "releases.json"
    path.write_text(json.dumps(DIGESTS), encoding="utf-8")
    return RuntimeSettings(state_dir=tmp_path / "queue-api-state", public_origin=ORIGIN, release_manifest_file=path, **overrides)


@pytest.fixture
def pilot_settings(tmp_path):
    return RuntimeSettings(state_dir=tmp_path / "closed-state", public_origin=ORIGIN)


@pytest.fixture
def open_settings(tmp_path):
    return queue_settings(tmp_path, queue_intake=True)


@pytest.fixture
def live_clients(identity, account_password):
    created = []

    def make(settings, username="operator_a"):
        client = TestClient(create_app(identity, settings=settings), base_url=ORIGIN, headers={"Origin": ORIGIN})
        client.__enter__()
        created.append(client)
        response = client.post("/api/auth/login", json={"username": username, "password": account_password})
        assert response.status_code == 200, response.text
        client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
        return client

    yield make
    for client in reversed(created):
        client.__exit__(None, None, None)


def queue_row_count(engine):
    with engine.connect() as connection:
        return connection.execute(select(func.count()).select_from(jobs).where(jobs.c.queue_kind.is_not(None))).scalar_one()


def test_intake_stays_closed_in_the_pilot(live_clients, identity_engine, pilot_settings):
    client = live_clients(pilot_settings)
    response = client.post("/api/prepare", json=VALID_POINT)
    assert response.status_code == 403
    assert response.json()["code"] == "pilot_operation_disabled"
    assert queue_row_count(identity_engine) == 0
    response = client.post("/api/region/refresh", json={"start": "2026-10"})
    assert response.status_code == 403
    assert queue_row_count(identity_engine) == 0


def test_capabilities_and_contracts_report_durable_queue_without_admission(live_clients, pilot_settings, tmp_path):
    client = live_clients(pilot_settings)
    payload = client.get("/api/capabilities").json()
    assert payload["operations"]["durable_queue"] is True
    assert payload["operations"]["queue_intake"] is False
    contracts = client.get("/api/contracts").json()
    assert contracts["x-pilot-computation-enabled"] is False
    assert contracts["paths"]["/api/prepare"]["post"]["x-pilot-admission"] == "disabled"
    assert contracts["x-durable-queue"]["admission"] == "disabled"
    open_client = live_clients(queue_settings(tmp_path / "open", queue_intake=True))
    assert open_client.get("/api/capabilities").json()["operations"]["queue_intake"] is True
    assert open_client.get("/api/contracts").json()["paths"]["/api/prepare"]["post"]["x-pilot-admission"] == "enabled"


def test_admission_creates_durable_job_with_full_identity(live_clients, identity_engine, open_settings):
    client = live_clients(open_settings)
    response = client.post("/api/prepare", json=VALID_POINT)
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert body["status_url"] == "/api/jobs/" + body["job"]
    assert response.headers["location"] == body["status_url"]
    assert response.headers["x-deduplicated"] == "false"
    assert response.headers["cache-control"] == "no-store"
    job_id = body["job"]
    again = client.post("/api/prepare", json=VALID_POINT)
    assert again.status_code == 202
    assert again.json()["job"] == job_id
    assert again.headers["x-deduplicated"] == "true"
    assert queue_row_count(identity_engine) == 1
    with identity_engine.connect() as connection:
        row = connection.execute(select(jobs).where(jobs.c.id == job_id)).mappings().one()
    assert row["queue_kind"] == "point_forecast"
    assert row["status"] == "queued"
    assert row["attempts"] == 0 and row["max_attempts"] == 3
    assert row["dedup_sha256"] and len(row["dedup_sha256"]) == 64
    params = row["data"]["params"]
    assert params["spec"]["start"] == "2026-10"
    assert params["identity"]["releases"] == DIGESTS
    assert params["identity"]["scope"]["namespace"] == "owned"
    assert params["config_snapshot"]["bundle_dir"].endswith("world")


def test_region_admission_creates_fan_out_parent(live_clients, identity_engine, open_settings):
    client = live_clients(open_settings)
    response = client.post("/api/region/refresh", json={"start": "2026-10"})
    assert response.status_code == 202
    job_id = response.json()["job"]
    with identity_engine.connect() as connection:
        row = connection.execute(select(jobs).where(jobs.c.id == job_id)).mappings().one()
    assert row["queue_kind"] == "region_field"
    assert row["data"]["params"]["region"] == "krai"
    assert len(row["data"]["params"]["grid_sha256"]) == 64
    assert row["data"]["params"]["identity"]["operation"] == "region"


def test_validation_and_roles_keep_priority_order(live_clients, open_settings):
    reader = live_clients(open_settings, username="reader_a")
    response = reader.post("/api/prepare", json={"invalid": True})
    assert response.status_code == 403
    assert response.json()["code"] == "role_forbidden"
    operator = live_clients(open_settings)
    response = operator.post("/api/prepare", json={**VALID_POINT, "start": "2026-13"})
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_request"
    response = operator.post("/api/prepare", json={**VALID_POINT, "region": "rostov"})
    assert response.status_code == 403
    assert response.json()["code"] == "pilot_region_disabled"
    response = operator.post("/api/prepare", json={**VALID_POINT, "lat": 45.03, "lon": 39.07})
    assert response.status_code == 403
    assert response.json()["code"] == "pilot_point_disabled"


def test_quota_and_depth_survive_through_http(live_clients, tmp_path):
    strict = queue_settings(tmp_path / "strict", queue_intake=True, queue_max_active_per_user=1)
    operator = live_clients(strict)
    assert operator.post("/api/prepare", json=VALID_POINT).status_code == 202
    response = operator.post("/api/prepare", json={**VALID_POINT, "start": "2026-11"})
    assert response.status_code == 429
    assert response.json()["code"] == "job_quota_exceeded"
    assert int(response.headers["retry-after"]) == strict.queue_retry_seconds
    saturated = queue_settings(tmp_path / "saturated", queue_intake=True, queue_max_queued=2, queue_max_active_per_user=20)
    deep = live_clients(saturated, username="operator_b")
    assert deep.post("/api/prepare", json={**VALID_POINT, "start": "2027-01"}).status_code == 202
    response = deep.post("/api/prepare", json={**VALID_POINT, "start": "2027-02"})
    assert response.status_code == 503
    assert response.json()["code"] == "queue_saturated"
    assert int(response.headers["retry-after"]) == saturated.queue_lease_seconds


def test_admission_requires_release_manifest(live_clients, identity_engine, tmp_path):
    naked = RuntimeSettings(state_dir=tmp_path / "naked", public_origin=ORIGIN, queue_intake=True)
    client = live_clients(naked)
    response = client.post("/api/prepare", json=VALID_POINT)
    assert response.status_code == 503
    assert response.json()["code"] == "release_identity_unavailable"
    assert queue_row_count(identity_engine) == 0


def test_cancel_and_events_are_owner_scoped(live_clients, open_settings):
    owner = live_clients(open_settings)
    job_id = owner.post("/api/prepare", json=VALID_POINT).json()["job"]
    events = owner.get(f"/api/jobs/{job_id}/events").json()["events"]
    assert [event["kind"] for event in events] == ["queued"]
    assert events[0]["sequence"] == 1
    response = owner.post(f"/api/jobs/{job_id}/cancel", json={"reason": "not needed anymore"})
    assert response.status_code == 200
    view = response.json()["job"]
    assert view["status"] == "cancelled"
    assert view["id"] == job_id
    assert view["lease_active"] is False
    updated = owner.get(f"/api/jobs/{job_id}/events").json()["events"]
    assert [event["kind"] for event in updated] == ["queued", "cancelled"]
    assert updated[1]["payload"]["reason"] == "not needed anymore"
    assert owner.get(f"/api/jobs/{job_id}/events?since=-1").status_code == 422
    assert owner.get(f"/api/jobs/{job_id}/events?limit=0").status_code == 422
    conflict = owner.post(f"/api/jobs/{job_id}/cancel", json={})
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "job_state_conflict"
    foreign = live_clients(open_settings, username="operator_b")
    assert foreign.get(f"/api/jobs/{job_id}/events").status_code == 404
    assert foreign.get(f"/api/queue/jobs/{job_id}").status_code == 404
    assert foreign.post(f"/api/jobs/{job_id}/cancel", json={}).status_code == 404


def test_reader_cannot_cancel_and_anonymous_cannot_read(live_clients, identity, open_settings):
    owner = live_clients(open_settings)
    job_id = owner.post("/api/prepare", json=VALID_POINT).json()["job"]
    reader = live_clients(open_settings, username="reader_a")
    response = reader.post(f"/api/jobs/{job_id}/cancel", json={})
    assert response.status_code == 403
    assert response.json()["code"] == "role_forbidden"
    anonymous = TestClient(create_app(identity, settings=open_settings), base_url=ORIGIN)
    with anonymous as client:
        assert client.get(f"/api/jobs/{job_id}/events").status_code == 401
        assert client.get("/api/queue/stats").status_code == 401


def test_two_replicas_return_one_status(live_clients, identity, tmp_path):
    left = live_clients(queue_settings(tmp_path / "replica-a", queue_intake=True))
    right = live_clients(queue_settings(tmp_path / "replica-b", queue_intake=True))
    job_id = left.post("/api/prepare", json=VALID_POINT).json()["job"]
    snapshot_left = left.get(f"/api/queue/jobs/{job_id}").json()
    snapshot_right = right.get(f"/api/queue/jobs/{job_id}").json()
    assert snapshot_left == snapshot_right
    assert snapshot_left["job"]["status"] == "queued"
    cancel = right.post(f"/api/jobs/{job_id}/cancel", json={})
    assert cancel.status_code == 200
    assert left.get(f"/api/queue/jobs/{job_id}").json()["job"]["status"] == "cancelled"


def test_legacy_job_rows_have_no_queue_face(live_clients, identity_engine, open_settings):
    with identity_engine.connect() as connection:
        owner_row = connection.execute(select(users.c.id, users.c.organization_id).where(users.c.username == "operator_a")).first()
    job_id = str(uuid4())
    with identity_engine.begin() as connection:
        connection.execute(insert(jobs).values(
            id=job_id, owner_id=owner_row[0], organization_id=owner_row[1],
            data={"params": {}, "log": []}, status="succeeded", created_at=1, updated_at=1,
        ))
    client = live_clients(open_settings)
    assert client.get(f"/api/jobs/{job_id}").status_code == 200
    assert client.get(f"/api/queue/jobs/{job_id}").status_code == 404
    assert client.post(f"/api/jobs/{job_id}/cancel", json={}).status_code == 409
    assert client.get(f"/api/jobs/{job_id}/events").json()["events"] == []


def test_queue_stats_are_admin_only(live_clients, open_settings):
    operator = live_clients(open_settings)
    assert operator.get("/api/queue/stats").status_code == 403
    admin = live_clients(open_settings, username="admin_a")
    stats = admin.get("/api/queue/stats").json()
    assert stats["global_slots"] == 2
    assert stats["intake_enabled"] is True
    assert all(key in stats for key in ("queued", "running", "succeeded", "failed", "cancelled", "expired_leases", "oldest_queued_age_s"))


def test_mutating_endpoints_require_csrf_and_exact_origin(live_clients, open_settings):
    client = live_clients(open_settings)
    job_id = client.post("/api/prepare", json=VALID_POINT).json()["job"]
    csrf = client.headers.pop("X-CSRF-Token")
    response = client.post(f"/api/jobs/{job_id}/cancel", json={})
    assert response.status_code == 403
    assert response.json()["code"] == "csrf_failed"
    client.headers["X-CSRF-Token"] = csrf
    client.headers["Origin"] = "https://evil.example"
    response = client.post(f"/api/jobs/{job_id}/cancel", json={})
    assert response.status_code == 403
    assert response.json()["code"] == "origin_forbidden"
    client.headers["Origin"] = ORIGIN
    assert client.post(f"/api/jobs/{job_id}/cancel", json={}).status_code == 200
