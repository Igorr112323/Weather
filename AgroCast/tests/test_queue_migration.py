import time
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, inspect, insert, select, text

from agrocast.identity.database import check_schema, migrate
from agrocast.identity.schema import REVISION, jobs, organizations, users

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
LEGACY_COLUMNS = {"id", "organization_id", "owner_id", "data", "created_at", "updated_at", "status"}
QUEUE_ONLY = {"queue_kind", "dedup_sha256", "attempts", "max_attempts", "next_retry_at", "lease_owner",
              "lease_expires_at", "heartbeat_at", "deadline_at", "cancel_requested_at", "parent_id",
              "started_at", "finished_at", "result_checksum"}


def _config():
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    return config


@pytest.fixture
def roundtrip_engine(tmp_path):
    engine = create_engine("sqlite:///" + str(tmp_path / "roundtrip.db"))

    @event.listens_for(engine, "connect")
    def _connect(dbapi_connection, record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    yield engine
    engine.dispose()


def _downgrade(engine, revision):
    with engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            connection.execute(text("SELECT pg_advisory_xact_lock(482076011)"))
        config = _config()
        config.attributes["connection"] = connection
        command.downgrade(config, revision)


def _job_row(organization_id, owner_id, queue=None, status="succeeded"):
    now = int(time.time())
    row = {
        "id": str(uuid4()), "organization_id": organization_id, "owner_id": owner_id,
        "status": status, "data": {"kind": "legacy", "queue": queue},
        "created_at": now, "updated_at": now,
    }
    if queue is not None:
        row.update({"queue_kind": "noop", "dedup_sha256": queue, "max_attempts": 2})
    return row


def _seed_identity(engine):
    organization_id = str(uuid4())
    user_id = str(uuid4())
    now = int(time.time())
    with engine.begin() as connection:
        connection.execute(insert(organizations).values(
            id=organization_id, name="org-" + user_id[:8], active=True, created_at=now))
        connection.execute(insert(users).values(
            id=user_id, organization_id=organization_id, username="mig_" + user_id[:8],
            password_hash="x", role="operator", active=True, created_at=now))
    return organization_id, user_id


def test_upgrade_from_0003_adds_queue_state_and_roundtrips(roundtrip_engine):
    _downgrade_guard = roundtrip_engine
    migrate(_downgrade_guard, "0003_persistent_state")
    organization_id, user_id = _seed_identity(_downgrade_guard)
    legacy = _job_row(organization_id, user_id)
    with _downgrade_guard.begin() as connection:
        connection.execute(insert(jobs).values(**legacy))
    columns = {column["name"] for column in inspect(_downgrade_guard).get_columns("jobs")}
    assert QUEUE_ONLY.isdisjoint(columns)

    migrate(_downgrade_guard)
    check_schema(_downgrade_guard)
    columns = {column["name"] for column in inspect(_downgrade_guard).get_columns("jobs")}
    assert QUEUE_ONLY <= columns
    with _downgrade_guard.connect() as connection:
        stored = connection.execute(select(jobs).where(jobs.c.id == legacy["id"])).mappings().one()
        index_names = {index["name"] for index in inspect(_downgrade_guard).get_indexes("jobs")}
        assert connection.execute(select(organizations.c.id)).scalars().all()
    assert stored.status == "succeeded"
    assert stored.data == legacy["data"]
    assert stored.attempts == 0
    assert stored.max_attempts == 3
    assert stored.queue_kind is None
    assert {"ix_jobs_claim", "uq_jobs_active_dedup", "ix_jobs_lease_expiry"} <= index_names

    queue_row = _job_row(organization_id, user_id, queue="b" * 64, status="queued")
    with _downgrade_guard.begin() as connection:
        connection.execute(insert(jobs).values(**queue_row))

    _downgrade(_downgrade_guard, "0003_persistent_state")
    columns = {column["name"] for column in inspect(_downgrade_guard).get_columns("jobs")}
    assert QUEUE_ONLY.isdisjoint(columns)
    with _downgrade_guard.connect() as connection:
        remaining = connection.execute(select(jobs.c.id).where(jobs.c.id == legacy["id"])).scalars().all()
    assert remaining == [legacy["id"]]

    migrate(_downgrade_guard)
    with _downgrade_guard.connect() as connection:
        version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        survived = connection.execute(select(jobs.c.id, jobs.c.status, jobs.c.data, jobs.c.queue_kind)).mappings().all()
    assert version == REVISION
    stored_rows = {row.id: row for row in survived}
    assert len(stored_rows) == 2
    assert stored_rows[legacy["id"]].data == legacy["data"]
    assert stored_rows[queue_row["id"]].queue_kind is None
    assert stored_rows[queue_row["id"]].data == queue_row["data"]


def test_active_dedup_partial_index_blocks_parallel_admissions(roundtrip_engine):
    migrate(roundtrip_engine)
    organization_id, user_id = _seed_identity(roundtrip_engine)
    digest = "c" * 64
    with pytest.raises(Exception) as first_error:
        with roundtrip_engine.begin() as connection:
            connection.execute(insert(jobs).values(**_job_row(organization_id, user_id, queue=digest, status="queued")))
            connection.execute(insert(jobs).values(**_job_row(organization_id, user_id, queue=digest, status="running")))
    assert "uq_jobs_active_dedup" in str(first_error.value) or "UNIQUE" in str(first_error.value).upper()
    with roundtrip_engine.begin() as connection:
        connection.execute(jobs.delete())
        connection.execute(insert(jobs).values(**_job_row(organization_id, user_id, queue=digest, status="queued")))
        connection.execute(insert(jobs).values(**_job_row(organization_id, user_id, queue=digest, status="cancelled")))
        terminal_again = _job_row(organization_id, user_id, queue=digest, status="failed")
        terminal_again["id"] = str(uuid4())
        connection.execute(insert(jobs).values(**terminal_again))
    with roundtrip_engine.connect() as connection:
        assert connection.execute(select(jobs.c.id).where(jobs.c.dedup_sha256 == digest)).scalars().all()


def test_events_are_sequence_monotonic_per_job(roundtrip_engine):
    migrate(roundtrip_engine)
    organization_id, user_id = _seed_identity(roundtrip_engine)
    from agrocast.identity.schema import queue_events

    job = _job_row(organization_id, user_id, queue="d" * 64, status="queued")
    with roundtrip_engine.begin() as connection:
        connection.execute(insert(jobs).values(**job))
        for sequence in (1, 2):
            connection.execute(insert(queue_events).values(
                id=str(uuid4()), job_id=job["id"], sequence=sequence, kind="log", payload={"n": sequence},
                created_at=int(time.time())))
        with pytest.raises(Exception) as duplicate:
            connection.execute(insert(queue_events).values(
                id=str(uuid4()), job_id=job["id"], sequence=2, kind="log", payload={"n": 2},
                created_at=int(time.time())))
    assert "uq_queue_event_sequence" in str(duplicate.value) or "UNIQUE" in str(duplicate.value).upper()


def test_head_migration_matches_identity_engine_state(identity_engine):
    check_schema(identity_engine)
    inspector = inspect(identity_engine)
    assert "queue_events" in inspector.get_table_names()
    columns = {column["name"] for column in inspector.get_columns("jobs")}
    assert QUEUE_ONLY <= columns
    event_indexes = {index["name"] for index in inspector.get_indexes("queue_events")}
    assert "ix_queue_events_job_id" in event_indexes
