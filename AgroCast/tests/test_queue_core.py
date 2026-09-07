import time
from uuid import uuid4

import pytest
from sqlalchemy import func, insert, select, update

from agrocast.core.settings import RuntimeSettings
from agrocast.identity.credentials import IdentityError
from agrocast.identity.schema import jobs, organizations, publications, queue_events, users
from agrocast.queue.service import JobQueue, QueueError, LeaseLost
from agrocast.store.results import fingerprint


def make_principal(engine, username=None, role="operator"):
    with engine.connect() as connection:
        if username is None:
            row = connection.execute(select(users.c.id, users.c.organization_id).where(users.c.role == role)).first()
            if row is not None:
                return _Actor(row[0], row[1])
        organization_id = str(uuid4())
        user_id = str(uuid4())
    with engine.begin() as connection:
        connection.execute(insert(organizations).values(id=organization_id, name="org-" + user_id[:8], active=True, created_at=int(time.time())))
        connection.execute(insert(users).values(id=user_id, organization_id=organization_id, username="q_" + user_id[:10], password_hash="x", role=role, active=True, created_at=int(time.time())))
    return _Actor(user_id, organization_id)


class _Actor:
    def __init__(self, user_id, organization_id):
        self.id = user_id
        self.organization_id = organization_id
        self.role = None
        self.session_hash = ""
        self.username = "queue-test"

    def public(self):
        return {}


@pytest.fixture
def settings(tmp_path):
    configuration = RuntimeSettings(state_dir=tmp_path / "queue-state")
    configuration.prepare_state()
    return configuration


@pytest.fixture
def queue(identity_engine, settings):
    return JobQueue(identity_engine, settings)


def test_enqueue_claim_complete_writes_events_and_publication(identity_engine, queue):
    actor = make_principal(identity_engine)
    result = queue.enqueue(actor, "noop", {"echo": 7}, dedup_sha256="a" * 64)
    claimed = queue.claim("worker-1")
    assert claimed["id"] == result["job_id"] and claimed["status"] == "running"
    assert claimed["attempts"] == 1 and claimed["lease_owner"] == "worker-1"
    queue.heartbeat(claimed["id"], "worker-1", log_lines=["step one", "step two"])
    completion = queue.complete(claimed["id"], "worker-1", {"answer": 42})
    assert completion["status"] == "succeeded"
    with identity_engine.connect() as connection:
        row = connection.execute(select(jobs).where(jobs.c.id == result["job_id"])).mappings().one()
        events = connection.execute(select(queue_events).where(queue_events.c.job_id == result["job_id"]).order_by(queue_events.c.sequence)).mappings().all()
    assert row["status"] == "succeeded"
    assert row["result_checksum"] == fingerprint({"answer": 42})
    assert row["data"]["report"] == {"answer": 42}
    assert row["data"]["log"] == ["step one", "step two"]
    assert [event["kind"] for event in events] == ["queued", "claimed", "succeeded"]
    assert [event["sequence"] for event in events] == [1, 2, 3]


def test_active_duplicates_are_deduplicated_and_terminal_allows_new_job(identity_engine, queue):
    actor = make_principal(identity_engine)
    first = queue.enqueue(actor, "noop", {}, dedup_sha256="d" * 64)
    second = queue.enqueue(actor, "noop", {}, dedup_sha256="d" * 64)
    assert second["deduplicated"] and second["job_id"] == first["job_id"]
    claimed = queue.claim("w")
    queue.complete(claimed["id"], "w", {"done": True})
    third = queue.enqueue(actor, "noop", {}, dedup_sha256="d" * 64)
    assert not third["deduplicated"] and third["job_id"] != first["job_id"]


def test_per_user_quota_returns_429_with_retry_after(identity_engine, settings):
    limited = RuntimeSettings(state_dir=settings.state_dir, queue_max_active_per_user=1)
    queue = JobQueue(identity_engine, limited)
    actor = make_principal(identity_engine)
    queue.enqueue(actor, "noop", {}, dedup_sha256="1" * 64)
    with pytest.raises(QueueError) as raised:
        queue.enqueue(actor, "noop", {}, dedup_sha256="2" * 64)
    assert raised.value.status == 429
    assert raised.value.retry_after == limited.queue_retry_seconds


def test_global_depth_returns_503_with_retry_after(identity_engine, settings):
    limited = RuntimeSettings(state_dir=settings.state_dir, queue_max_queued=1, queue_max_active_per_user=50)
    queue = JobQueue(identity_engine, limited)
    actor_a = make_principal(identity_engine)
    actor_b = make_principal(identity_engine)
    queue.enqueue(actor_a, "noop", {}, dedup_sha256="3" * 64)
    with pytest.raises(QueueError) as raised:
        queue.enqueue(actor_b, "noop", {}, dedup_sha256="4" * 64)
    assert raised.value.status == 503
    assert raised.value.retry_after == limited.queue_lease_seconds


def test_global_slots_limit_running_jobs(identity_engine, settings):
    capped = RuntimeSettings(state_dir=settings.state_dir, queue_global_slots=1, queue_max_active_per_user=10)
    queue = JobQueue(identity_engine, capped)
    actor = make_principal(identity_engine)
    queue.enqueue(actor, "noop", {}, dedup_sha256="5" * 64)
    queue.enqueue(actor, "noop", {}, dedup_sha256="6" * 64)
    first = queue.claim("solo")
    assert first is not None
    assert queue.claim("other") is None
    queue.complete(first["id"], "solo", {"v": 1})
    second = queue.claim("other")
    assert second is not None


def test_retry_backoff_exhausts_attempts_then_fails(identity_engine, queue):
    actor = make_principal(identity_engine)
    enqueued = queue.enqueue(actor, "noop", {}, dedup_sha256="7" * 64)
    for attempt in (1, 2, 3):
        claimed = queue.claim("w")
        assert claimed["id"] == enqueued["job_id"]
        assert claimed["attempts"] == attempt
        outcome = queue.fail(claimed["id"], "w", "compute_failed", "synthetic")
        if attempt < queue.settings.queue_max_attempts:
            with identity_engine.connect() as connection:
                row = connection.execute(select(jobs).where(jobs.c.id == enqueued["job_id"])).mappings().one()
            assert outcome["status"] == "queued"
            assert row["next_retry_at"] is not None and row["next_retry_at"] > int(time.time())
            with identity_engine.begin() as connection:
                connection.execute(update(jobs).where(jobs.c.id == enqueued["job_id"]).values(next_retry_at=None))
        else:
            assert outcome["status"] == "failed"
    with identity_engine.connect() as connection:
        row = connection.execute(select(jobs).where(jobs.c.id == enqueued["job_id"])).mappings().one()
        events = connection.execute(select(queue_events).where(queue_events.c.job_id == enqueued["job_id"]).order_by(queue_events.c.sequence)).mappings().all()
    assert row["status"] == "failed"
    assert row["data"]["error_code"] == "compute_failed"
    assert events[-1]["kind"] == "failed"


def test_expired_lease_requeues_with_backoff_and_finalises(identity_engine, queue):
    actor = make_principal(identity_engine)
    enqueued = queue.enqueue(actor, "noop", {}, dedup_sha256="8" * 64)
    claimed = queue.claim("ghost")
    with identity_engine.begin() as connection:
        connection.execute(update(jobs).where(jobs.c.id == enqueued["job_id"]).values(lease_expires_at=0))
    recovered = queue.reap_expired_leases()
    assert recovered == [(enqueued["job_id"], "requeued")]
    with identity_engine.connect() as connection:
        row = connection.execute(select(jobs).where(jobs.c.id == enqueued["job_id"])).mappings().one()
    assert row["status"] == "queued" and row["lease_owner"] is None and row["next_retry_at"] > 0
    with identity_engine.begin() as connection:
        connection.execute(update(jobs).where(jobs.c.id == enqueued["job_id"]).values(next_retry_at=None))
    for _ in range(queue.settings.queue_max_attempts - 1):
        with identity_engine.begin() as connection:
            connection.execute(update(jobs).where(jobs.c.id == enqueued["job_id"]).values(next_retry_at=None))
        claimed = queue.claim("ghost")
        assert claimed is not None
        with identity_engine.begin() as connection:
            connection.execute(update(jobs).where(jobs.c.id == enqueued["job_id"]).values(lease_expires_at=0))
        queue.reap_expired_leases()
    with identity_engine.connect() as connection:
        row = connection.execute(select(jobs).where(jobs.c.id == enqueued["job_id"])).mappings().one()
    assert row["status"] == "failed"
    assert row["data"]["error_code"] == "lease_expired"


def test_heartbeat_and_fail_reject_stale_owners(identity_engine, queue):
    actor = make_principal(identity_engine)
    enqueued = queue.enqueue(actor, "noop", {}, dedup_sha256="9" * 64)
    queue.claim("holder")
    with pytest.raises(LeaseLost):
        queue.heartbeat(enqueued["job_id"], "thief")
    with pytest.raises(LeaseLost):
        queue.fail(enqueued["job_id"], "thief", "x", "y")
    with pytest.raises(LeaseLost):
        queue.complete(enqueued["job_id"], "thief", {})


def test_cancel_queued_and_request_cancel_running(identity_engine, queue):
    actor = make_principal(identity_engine)
    queued = queue.enqueue(actor, "noop", {}, dedup_sha256="b" * 64)
    result = queue.cancel(actor, queued["job_id"])
    assert result["status"] == "cancelled"
    with pytest.raises(QueueError) as conflict:
        queue.cancel(actor, queued["job_id"])
    assert conflict.value.status == 409
    running = queue.enqueue(actor, "noop", {}, dedup_sha256="c" * 64)
    queue.claim("w")
    result = queue.cancel(actor, running["job_id"])
    assert result == {"status": "running", "cancel_requested": True}
    with identity_engine.connect() as connection:
        row = connection.execute(select(jobs).where(jobs.c.id == running["job_id"])).mappings().one()
    assert row["status"] == "running" and row["cancel_requested_at"] is not None
    queue.finalize_cancelled(running["job_id"], "w")
    with identity_engine.connect() as connection:
        row = connection.execute(select(jobs).where(jobs.c.id == running["job_id"])).mappings().one()
    assert row["status"] == "cancelled"


def test_completion_during_cancel_discards_result(identity_engine, queue):
    actor = make_principal(identity_engine)
    enqueued = queue.enqueue(actor, "point_forecast", {}, dedup_sha256="e" * 64)
    queue.claim("w")
    queue.cancel(actor, enqueued["job_id"])
    outcome = queue.complete(enqueued["job_id"], "w", {"late": True})
    assert outcome == {"status": "cancelled"}
    with identity_engine.connect() as connection:
        row = connection.execute(select(jobs).where(jobs.c.id == enqueued["job_id"])).mappings().one()
        publication_rows = connection.execute(select(publications).where(publications.c.job_id == enqueued["job_id"])).mappings().all()
    assert row["status"] == "cancelled" and row["result_checksum"] is None
    assert row["data"].get("report") is None
    assert publication_rows == []


def test_idempotent_publish_keeps_single_publication(identity_engine, queue):
    actor = make_principal(identity_engine)
    enqueued = queue.enqueue(actor, "point_forecast", {}, dedup_sha256="f" * 63 + "0")
    claimed = queue.claim("w")
    queue.complete(claimed["id"], "w", {"v": 1})
    with pytest.raises(LeaseLost):
        queue.complete(enqueued["job_id"], "w", {"v": 1})
    with identity_engine.connect() as connection:
        rows = connection.execute(select(publications).where(publications.c.job_id == enqueued["job_id"])).mappings().all()
    assert len(rows) == 1
    assert rows[0]["checksum"] == fingerprint({"v": 1})
    assert rows[0]["data"]["kind"] == "point_forecast"


def test_region_parent_waits_for_children_and_cascades_failure(identity_engine, settings):
    capped = RuntimeSettings(state_dir=settings.state_dir, queue_global_slots=1, queue_max_active_per_user=20)
    queue = JobQueue(identity_engine, capped)
    actor = make_principal(identity_engine)
    parent = queue.enqueue(actor, "region_field", {"start": "2026-10", "region": "krai", "grid_sha256": "f" * 64}, dedup_sha256="1" * 63 + "0")
    claimed_parent = queue.claim("w")
    assert claimed_parent["id"] == parent["job_id"]
    from agrocast.queue.worker import _OwnerActor

    child_one = queue.enqueue(_OwnerActor(actor.id, actor.organization_id), "region_cell", {"cell": {"id": "P01", "lat": 46.25, "lon": 38.25}}, dedup_sha256="2" * 63 + "0", parent_id=parent["job_id"])
    child_two = queue.enqueue(_OwnerActor(actor.id, actor.organization_id), "region_cell", {"cell": {"id": "P02", "lat": 46.25, "lon": 38.75}}, dedup_sha256="3" * 63 + "0", parent_id=parent["job_id"])
    queue.park(parent["job_id"], "w", {"phase": "cells"})
    child_ids = {child_one["job_id"], child_two["job_id"]}
    first_child = queue.claim("w")
    assert first_child["id"] in child_ids
    assert queue.claim("w2") is None
    queue.complete(first_child["id"], "w", {"below": 0.34, "normal": 0.33, "above": 0.33})
    second_child = queue.claim("w")
    assert second_child["id"] == (child_ids - {first_child["id"]}).pop()
    assert queue.claim("w3") is None
    queue.fail(second_child["id"], "w", "compute_failed", "cell broke", retryable=False)
    with identity_engine.connect() as connection:
        parent_row = connection.execute(select(jobs).where(jobs.c.id == parent["job_id"])).mappings().one()
        events = connection.execute(select(queue_events.c.kind).where(queue_events.c.job_id == parent["job_id"])).scalars().all()
        parent_publication = connection.execute(select(publications.c.id).where(publications.c.job_id == parent["job_id"])).first()
    assert parent_row["status"] == "failed"
    assert "failed" in events
    assert parent_publication is None


def test_stats_and_events_are_scoped_and_capped(identity_engine, queue):
    actor = make_principal(identity_engine)
    enqueued = queue.enqueue(actor, "noop", {"x": 1}, dedup_sha256="4" * 63 + "1")
    queue.claim("w")
    stats = queue.stats(None)
    assert stats["queued"] + stats["running"] >= 1
    assert stats["global_slots"] == 2 and stats["intake_enabled"] is False
    events = queue.list_events(enqueued["job_id"], since=1)
    assert [event["sequence"] for event in events] == [2]
    assert queue.list_events(enqueued["job_id"], since=99) == []


def test_sweep_removes_events_and_orphan_jobs_without_publication(identity_engine, settings):
    short = RuntimeSettings(state_dir=settings.state_dir, queue_retention_days=1)
    queue = JobQueue(identity_engine, short)
    actor = make_principal(identity_engine)
    pinned = queue.enqueue(actor, "point_forecast", {}, dedup_sha256="5" * 63 + "2")
    claimed = queue.claim("w")
    queue.complete(claimed["id"], "w", {"done": 1})
    other = queue.enqueue(actor, "noop", {}, dedup_sha256="5" * 63 + "3")
    claimed_other = queue.claim("w")
    queue.fail(claimed_other["id"], "w", "compute_failed", "gone", retryable=False)
    assert pinned["job_id"] == claimed["id"]
    with identity_engine.begin() as connection:
        connection.execute(update(jobs).values(updated_at=1, created_at=1))
    report = queue.sweep(now=int(time.time()))
    assert report["events_deleted"] >= 4
    with identity_engine.connect() as connection:
        remaining = connection.execute(select(func.count()).select_from(jobs).where(jobs.c.id == pinned["job_id"])).scalar_one()
        gone = connection.execute(select(func.count()).select_from(jobs).where(jobs.c.id == other["job_id"])).scalar_one()
        events_left = connection.execute(select(func.count()).select_from(queue_events).where(queue_events.c.job_id == pinned["job_id"])).scalar_one()
    assert remaining == 1
    assert gone == 0
    assert events_left == 0


def test_legacy_rows_never_enter_the_queue(identity_engine, queue):
    actor = make_principal(identity_engine)
    now = int(time.time())
    with identity_engine.begin() as connection:
        connection.execute(insert(jobs).values(
            id=str(uuid4()), organization_id=actor.organization_id, owner_id=actor.id,
            data={"params": {}, "report": {"legacy": True}}, status="queued",
            created_at=now, updated_at=now,
        ))
    assert queue.claim("w") is None


def test_nonretryable_failure_finalises_immediately(identity_engine, queue):
    actor = make_principal(identity_engine)
    enqueued = queue.enqueue(actor, "noop", {}, dedup_sha256="6" * 63 + "3")
    queue.claim("w")
    outcome = queue.fail(enqueued["job_id"], "w", "sandbox_unavailable", "kernel refused", retryable=False)
    assert outcome["status"] == "failed"


def test_identity_errors_surface_retry_after_for_http_mapping(identity_engine, settings):
    strict = RuntimeSettings(state_dir=settings.state_dir, queue_max_active_per_user=1)
    queue = JobQueue(identity_engine, strict)
    actor = make_principal(identity_engine)
    queue.enqueue(actor, "noop", {}, dedup_sha256="7" * 63 + "4")
    try:
        queue.enqueue(actor, "noop", {}, dedup_sha256="8" * 63 + "5")
    except IdentityError as error:
        assert error.status == 429 and error.retry_after == strict.queue_retry_seconds
    else:
        pytest.fail("quota must raise")
