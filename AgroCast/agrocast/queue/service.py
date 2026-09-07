import time
from uuid import uuid4

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError

from agrocast.core.settings import RuntimeSettings
from agrocast.identity.credentials import IdentityError
from agrocast.identity.schema import jobs, publications, queue_events
from agrocast.identity.service import IdentityService, WRITE_ROLES
from agrocast.store.results import fingerprint

INLINE_REPORT_LIMIT = 512 * 1024
MAX_LOG_KEEP = 200


class QueueError(IdentityError):
    def __init__(self, code, status, message=None, retry_after=None):
        super().__init__(code, status, retry_after)
        self.message = message or code


class LeaseLost(QueueError):
    def __init__(self, job_id):
        super().__init__("lease_lost", 409, f"Lease for job {job_id} is no longer held")


def _now(clock):
    return int(clock())


def _backoff(settings, attempts):
    delay = settings.queue_retry_seconds * (2 ** max(0, attempts - 1))
    return min(delay, settings.queue_retry_cap_seconds)


def _append_log(current, new_lines, cap):
    lines = list(current or [])
    lines.extend(str(line) for line in new_lines)
    return lines[-cap:]


class JobQueue:
    def __init__(self, engine, settings: RuntimeSettings, identity: IdentityService | None = None, clock=time.time):
        self.engine = engine
        self.settings = settings
        self.identity = identity
        self.clock = clock

    def _now(self):
        return _now(self.clock)

    def _event(self, connection, job_id, kind, payload):
        last = connection.execute(select(func.max(queue_events.c.sequence)).where(
            queue_events.c.job_id == job_id,
        )).scalar()
        connection.execute(insert(queue_events).values(
            id=str(uuid4()), job_id=job_id, sequence=int(last or 0) + 1,
            kind=kind, payload=payload, created_at=self._now(),
        ))

    def _guard_actor(self, connection, principal, roles=WRITE_ROLES):
        if self.identity is None:
            return principal
        return self.identity._actor(connection, principal, roles)

    def _active_exists(self, connection, organization_id, dedup_sha256):
        row = connection.execute(select(jobs.c.id).where(
            jobs.c.organization_id == organization_id,
            jobs.c.dedup_sha256 == dedup_sha256,
            jobs.c.queue_kind.is_not(None),
            jobs.c.status.in_(("queued", "running")),
        )).first()
        return row[0] if row is not None else None

    def enqueue(self, principal, kind, params, dedup_sha256=None, parent_id=None):
        if kind not in self.accepted_kinds():
            raise QueueError("invalid_queue_kind", 422)
        if dedup_sha256 is not None and (len(dedup_sha256) != 64 or any(ch not in "0123456789abcdef" for ch in dedup_sha256)):
            raise QueueError("invalid_dedup_identity", 422)
        for attempt in range(2):
            try:
                with self.engine.begin() as connection:
                    current = self._guard_actor(connection, principal)
                    if parent_id is not None and kind not in JobQueue.child_kinds() and kind not in _test_kinds():
                        raise QueueError("invalid_queue_kind", 422)
                    active_owner = connection.execute(select(func.count()).select_from(jobs).where(
                        jobs.c.owner_id == current.id,
                        jobs.c.queue_kind.is_not(None),
                        jobs.c.status.in_(("queued", "running")),
                        jobs.c.parent_id.is_(None),
                    )).scalar_one()
                    if active_owner >= self.settings.queue_max_active_per_user:
                        raise QueueError("job_quota_exceeded", 429, "The per-account active job limit is reached", self.settings.queue_retry_seconds)
                    depth = connection.execute(select(func.count()).select_from(jobs).where(
                        jobs.c.queue_kind.is_not(None), jobs.c.status == "queued",
                    )).scalar_one()
                    if depth >= self.settings.queue_max_queued:
                        raise QueueError("queue_saturated", 503, "The durable queue is at its configured depth limit", self.settings.queue_lease_seconds)
                    if parent_id is None and dedup_sha256 is not None:
                        existing = self._active_exists(connection, current.organization_id, dedup_sha256)
                        if existing is not None:
                            return {"job_id": existing, "deduplicated": True}
                    job_id = str(uuid4())
                    now = self._now()
                    data = {"params": params, "log": []}
                    connection.execute(insert(jobs).values(
                        id=job_id, organization_id=current.organization_id, owner_id=current.id,
                        data=data, status="queued", queue_kind=kind, dedup_sha256=dedup_sha256,
                        max_attempts=self.settings.queue_max_attempts, created_at=now, updated_at=now,
                        parent_id=parent_id,
                    ))
                    self._event(connection, job_id, "queued", {"kind": kind, "attempts": 0})
                    return {"job_id": job_id, "deduplicated": False}
            except IntegrityError:
                with self.engine.connect() as connection:
                    existing = self._active_exists(connection, principal.organization_id, dedup_sha256)
                if existing is not None:
                    return {"job_id": existing, "deduplicated": True}
        raise QueueError("job_state_conflict", 409)

    def claim(self, worker_id, limit=None):
        now = self._now()
        slots = self.settings.queue_global_slots if limit is None else min(limit, self.settings.queue_global_slots)
        with self.engine.begin() as connection:
            running = connection.execute(select(func.count()).select_from(jobs).where(
                jobs.c.queue_kind.is_not(None), jobs.c.status == "running",
                jobs.c.lease_expires_at > now,
            )).scalar_one()
            available = slots - running
            if available <= 0:
                return None
            child = jobs.alias()
            pending_slots = available
            query = select(jobs.c.id).where(
                jobs.c.queue_kind.is_not(None), jobs.c.status == "queued",
                (jobs.c.next_retry_at.is_(None)) | (jobs.c.next_retry_at <= now),
                ~select(child.c.id).where(
                    child.c.parent_id == jobs.c.id,
                    child.c.status.notin_(("succeeded", "cancelled", "failed")),
                ).correlate(jobs).limit(1).exists(),
            ).order_by(jobs.c.created_at, jobs.c.id).limit(pending_slots)
            if connection.dialect.name == "postgresql":
                query = query.with_for_update(skip_locked=True)
            candidate_ids = [row[0] for row in connection.execute(query).all()]
            for job_id in candidate_ids:
                result = connection.execute(update(jobs).where(
                    jobs.c.id == job_id, jobs.c.status == "queued",
                ).values(
                    status="running", lease_owner=worker_id, lease_expires_at=now + self.settings.queue_lease_seconds,
                    heartbeat_at=now, started_at=now, finished_at=None, result_checksum=None,
                    deadline_at=now + self.settings.queue_deadline_seconds,
                    attempts=jobs.c.attempts + 1, updated_at=now,
                ))
                if result.rowcount == 1:
                    row = connection.execute(select(jobs).where(jobs.c.id == job_id)).mappings().one()
                    self._event(connection, job_id, "claimed", {"attempt": row["attempts"], "worker": worker_id})
                    return dict(row)
        return None

    def _touch_lease(self, connection, job_id, worker_id, extra):
        values = dict(extra)
        values.update({
            "lease_expires_at": self._now() + self.settings.queue_lease_seconds,
            "heartbeat_at": self._now(),
            "updated_at": self._now(),
        })
        result = connection.execute(update(jobs).where(
            jobs.c.id == job_id, jobs.c.lease_owner == worker_id, jobs.c.status == "running",
        ).values(**values))
        if result.rowcount != 1:
            raise LeaseLost(job_id)

    def heartbeat(self, job_id, worker_id, log_lines=None):
        with self.engine.begin() as connection:
            row = connection.execute(select(jobs.c.data).where(jobs.c.id == job_id)).mappings().first()
            if row is None:
                raise QueueError("resource_not_found", 404)
            data = dict(row["data"] or {})
            if log_lines:
                data["log"] = _append_log(data.get("log"), log_lines, self.settings.queue_log_lines)
            self._touch_lease(connection, job_id, worker_id, {"data": data})
        return True

    def current_state(self, job_id):
        with self.engine.connect() as connection:
            row = connection.execute(select(
                jobs.c.status, jobs.c.cancel_requested_at, jobs.c.lease_owner, jobs.c.lease_expires_at,
                jobs.c.deadline_at,
            ).where(jobs.c.id == job_id)).mappings().first()
            return dict(row) if row is not None else None

    def complete(self, job_id, worker_id, payload, result_ref=None):
        checksum = fingerprint(payload)
        now = self._now()
        with self.engine.begin() as connection:
            row = connection.execute(select(jobs).where(jobs.c.id == job_id)).mappings().first()
            if row is None:
                raise QueueError("resource_not_found", 404)
            if row["status"] != "running" or row["lease_owner"] != worker_id:
                raise LeaseLost(job_id)
            if row["cancel_requested_at"] is not None:
                connection.execute(update(jobs).where(
                    jobs.c.id == job_id, jobs.c.status == "running", jobs.c.lease_owner == worker_id,
                ).values(status="cancelled", finished_at=now, updated_at=now, lease_owner=None, lease_expires_at=None))
                self._event(connection, job_id, "cancelled", {"reason": "cancel_requested", "discard_result": True})
                return {"status": "cancelled"}
            data = dict(row["data"] or {})
            data["report"] = payload if _payload_size(payload) <= INLINE_REPORT_LIMIT else None
            if data.get("report") is None:
                data["report_ref"] = result_ref
            data["result"] = {"checksum": checksum, "inline": data.get("report") is not None}
            connection.execute(update(jobs).where(
                jobs.c.id == job_id, jobs.c.status == "running", jobs.c.lease_owner == worker_id,
            ).values(
                status="succeeded", data=data, result_checksum=checksum,
                finished_at=now, updated_at=now, lease_owner=None, lease_expires_at=None,
            ))
            self._event(connection, job_id, "succeeded", {"attempt": row["attempts"], "checksum": checksum})
            if row["queue_kind"] in JobQueue.top_level_kinds():
                already = connection.execute(select(publications.c.id).where(
                    publications.c.job_id == job_id,
                )).first()
                if already is None:
                    connection.execute(insert(publications).values(
                        id=str(uuid4()), organization_id=row["organization_id"], owner_id=row["owner_id"],
                        job_id=job_id, checksum=checksum, created_at=now, updated_at=now,
                        data={"kind": row["queue_kind"], "identity_sha256": row["dedup_sha256"], "result_checksum": checksum},
                    ))
                    self._event(connection, job_id, "published", {"checksum": checksum})
        return {"status": "succeeded", "checksum": checksum}

    def fail(self, job_id, worker_id, code, summary, retryable=True):
        summary = str(summary)[:500]
        now = self._now()
        with self.engine.begin() as connection:
            row = connection.execute(select(jobs).where(jobs.c.id == job_id)).mappings().first()
            if row is None:
                raise QueueError("resource_not_found", 404)
            if row["status"] != "running" or row["lease_owner"] != worker_id:
                raise LeaseLost(job_id)
            data = dict(row["data"] or {})
            data["error_code"] = code
            data["error_summary"] = summary
            can_retry = retryable and row["attempts"] < row["max_attempts"]
            if can_retry:
                next_retry = now + _backoff(self.settings, row["attempts"])
                connection.execute(update(jobs).where(
                    jobs.c.id == job_id, jobs.c.status == "running", jobs.c.lease_owner == worker_id,
                ).values(status="queued", data=data, next_retry_at=next_retry, updated_at=now,
                         lease_owner=None, lease_expires_at=None, deadline_at=None))
                self._event(connection, job_id, "retry_queued", {"attempt": row["attempts"], "code": code, "next_retry_at": next_retry})
            else:
                connection.execute(update(jobs).where(
                    jobs.c.id == job_id, jobs.c.status == "running", jobs.c.lease_owner == worker_id,
                ).values(status="failed", data=data, finished_at=now, updated_at=now,
                         lease_owner=None, lease_expires_at=None, deadline_at=None))
                self._event(connection, job_id, "failed", {"attempt": row["attempts"], "code": code})
                if row["parent_id"] is not None:
                    self._cascade_parent_failure(connection, row["parent_id"], job_id, now)
        return {"status": "queued" if can_retry else "failed"}

    def _cascade_parent_failure(self, connection, parent_id, child_id, now):
        parent = connection.execute(select(jobs).where(jobs.c.id == parent_id)).mappings().first()
        if parent is None or parent["status"] not in {"queued", "running"}:
            return
        if parent["status"] == "running":
            connection.execute(update(jobs).where(jobs.c.id == parent_id).values(
                status="failed", finished_at=now, updated_at=now, lease_owner=None, lease_expires_at=None, deadline_at=None,
            ))
        else:
            connection.execute(update(jobs).where(jobs.c.id == parent_id).values(
                status="failed", finished_at=now, updated_at=now, deadline_at=None,
            ))
        self._event(connection, parent_id, "failed", {"code": "dependency_failed", "child": child_id})
        siblings = connection.execute(select(jobs.c.id).where(
            jobs.c.parent_id == parent_id, jobs.c.id != child_id, jobs.c.status == "queued",
        )).scalars().all()
        for sibling_id in siblings:
            connection.execute(update(jobs).where(jobs.c.id == sibling_id).values(
                status="cancelled", finished_at=now, updated_at=now,
            ))
            self._event(connection, sibling_id, "cancelled", {"reason": "dependency_failed", "parent": parent_id})
        running_siblings = connection.execute(select(jobs.c.id).where(
            jobs.c.parent_id == parent_id, jobs.c.id != child_id, jobs.c.status == "running",
        )).scalars().all()
        if running_siblings:
            connection.execute(update(jobs).where(jobs.c.id.in_(running_siblings)).values(cancel_requested_at=now))

    def reap_expired_leases(self):
        now = self._now()
        recovered = []
        with self.engine.begin() as connection:
            expired = connection.execute(select(jobs.c.id).where(
                jobs.c.queue_kind.is_not(None), jobs.c.status == "running",
                (jobs.c.lease_expires_at <= now) | jobs.c.lease_expires_at.is_(None),
            )).scalars().all()
            for job_id in expired:
                row = connection.execute(select(jobs).where(jobs.c.id == job_id)).mappings().first()
                if row is None or row["status"] != "running":
                    continue
                if row["cancel_requested_at"] is not None:
                    connection.execute(update(jobs).where(jobs.c.id == job_id).values(
                        status="cancelled", finished_at=now, updated_at=now,
                        lease_owner=None, lease_expires_at=None, deadline_at=None,
                    ))
                    self._event(connection, job_id, "cancelled", {"reason": "worker_lost"})
                    recovered.append((job_id, "cancelled"))
                    continue
                data = dict(row["data"] or {})
                data["error_code"] = "lease_expired"
                data["error_summary"] = "The worker stopped updating this job; the lease expired and the attempt was counted"
                if row["attempts"] >= row["max_attempts"]:
                    connection.execute(update(jobs).where(jobs.c.id == job_id).values(
                        status="failed", data=data, finished_at=now, updated_at=now,
                        lease_owner=None, lease_expires_at=None, deadline_at=None,
                    ))
                    self._event(connection, job_id, "failed", {"code": "lease_expired", "attempt": row["attempts"]})
                    if row["parent_id"] is not None:
                        self._cascade_parent_failure(connection, row["parent_id"], job_id, now)
                    recovered.append((job_id, "failed"))
                else:
                    next_retry = now + _backoff(self.settings, row["attempts"])
                    connection.execute(update(jobs).where(jobs.c.id == job_id).values(
                        status="queued", data=data, next_retry_at=next_retry, updated_at=now,
                        lease_owner=None, lease_expires_at=None, deadline_at=None,
                    ))
                    self._event(connection, job_id, "requeued", {"code": "lease_expired", "next_retry_at": next_retry})
                    recovered.append((job_id, "requeued"))
        return recovered

    def cancel(self, principal, job_id, reason=None):
        now = self._now()
        payload = {"reason": reason or "operator_cancel"}
        with self.engine.begin() as connection:
            current = self._guard_actor(connection, principal)
            row = connection.execute(select(jobs).where(
                jobs.c.id == job_id, jobs.c.organization_id == current.organization_id, jobs.c.owner_id == current.id,
            )).mappings().first()
            if row is None:
                raise QueueError("resource_not_found", 404)
            if row["queue_kind"] is None:
                raise QueueError("job_state_conflict", 409)
            if row["status"] not in {"queued", "running"}:
                raise QueueError("job_state_conflict", 409)
            if row["status"] == "queued":
                connection.execute(update(jobs).where(jobs.c.id == job_id, jobs.c.status == "queued").values(
                    status="cancelled", finished_at=now, updated_at=now,
                ))
                self._event(connection, job_id, "cancelled", payload)
                self._cancel_queued_children(connection, job_id, now)
                self._cascade_parent_cancel(connection, row, now)
                return {"status": "cancelled"}
            connection.execute(update(jobs).where(jobs.c.id == job_id).values(cancel_requested_at=now, updated_at=now))
            self._event(connection, job_id, "cancellation_requested", payload)
            self._cancel_queued_children(connection, job_id, now)
            return {"status": "running", "cancel_requested": True}

    def _cascade_parent_cancel(self, connection, row, now):
        if row["parent_id"] is None:
            return
        parent = connection.execute(select(jobs).where(jobs.c.id == row["parent_id"])).mappings().first()
        if parent is None or parent["status"] != "running":
            return
        connection.execute(update(jobs).where(jobs.c.id == parent["id"]).values(cancel_requested_at=now, updated_at=now))
        self._event(connection, parent["id"], "cancellation_requested", {"reason": "child_cancelled"})

    def _cancel_queued_children(self, connection, parent_id, now):
        children = connection.execute(select(jobs.c.id).where(
            jobs.c.parent_id == parent_id, jobs.c.status == "queued",
        )).scalars().all()
        for child_id in children:
            connection.execute(update(jobs).where(jobs.c.id == child_id).values(
                status="cancelled", finished_at=now, updated_at=now,
            ))
            self._event(connection, child_id, "cancelled", {"reason": "parent_cancelled", "parent": parent_id})

    def finalize_cancelled(self, job_id, worker_id):
        now = self._now()
        with self.engine.begin() as connection:
            result = connection.execute(update(jobs).where(
                jobs.c.id == job_id, jobs.c.status == "running", jobs.c.lease_owner == worker_id,
            ).values(status="cancelled", finished_at=now, updated_at=now,
                     lease_owner=None, lease_expires_at=None, deadline_at=None))
            if result.rowcount != 1:
                raise LeaseLost(job_id)
            self._event(connection, job_id, "cancelled", {"reason": "worker_observed_cancel"})

    def park(self, job_id, worker_id, data_patch, delay=0):
        now = self._now()
        with self.engine.begin() as connection:
            row = connection.execute(select(jobs.c.data).where(jobs.c.id == job_id)).mappings().first()
            if row is None:
                raise QueueError("resource_not_found", 404)
            data = dict(row["data"] or {})
            data.update(data_patch)
            self._touch_lease(connection, job_id, worker_id, {"data": data})
            connection.execute(update(jobs).where(jobs.c.id == job_id).values(
                status="queued", next_retry_at=now + delay, lease_owner=None, lease_expires_at=None, deadline_at=None,
            ))
            self._event(connection, job_id, "parked", {"delay": delay, **{k: v for k, v in data_patch.items() if k != "log"}})

    def durable_view(self, row):
        data = dict(row.get("data") or {})
        return {
            "id": row["id"], "status": row["status"], "queue_kind": row["queue_kind"],
            "attempts": row["attempts"], "max_attempts": row["max_attempts"],
            "next_retry_at": row["next_retry_at"], "deadline_at": row["deadline_at"],
            "lease_active": row["status"] == "running", "cancel_requested": row["cancel_requested_at"] is not None,
            "started_at": row["started_at"], "finished_at": row["finished_at"],
            "result_checksum": row["result_checksum"], "error_code": data.get("error_code"),
            "parent_id": row["parent_id"], "log": data.get("log", [])[-20:],
        }

    def list_events(self, job_id, since=0, limit=200):
        with self.engine.connect() as connection:
            rows = connection.execute(select(queue_events).where(
                queue_events.c.job_id == job_id, queue_events.c.sequence > since,
            ).order_by(queue_events.c.sequence).limit(limit)).mappings().all()
            return [dict(row) for row in rows]

    def stats(self, principal):
        with self.engine.connect() as connection:
            if self.identity is not None:
                self.identity._actor(connection, principal, frozenset({"admin"}))
            counts = dict(connection.execute(select(jobs.c.status, func.count()).where(
                jobs.c.queue_kind.is_not(None),
            ).group_by(jobs.c.status)).all())
            oldest = connection.execute(select(func.min(jobs.c.created_at)).where(
                jobs.c.queue_kind.is_not(None), jobs.c.status == "queued",
            )).scalar()
            expired = connection.execute(select(func.count()).select_from(jobs).where(
                jobs.c.queue_kind.is_not(None), jobs.c.status == "running",
                jobs.c.lease_expires_at <= self._now(),
            )).scalar_one()
            return {
                "queued": int(counts.get("queued", 0)), "running": int(counts.get("running", 0)),
                "succeeded": int(counts.get("succeeded", 0)), "failed": int(counts.get("failed", 0)),
                "cancelled": int(counts.get("cancelled", 0)), "expired_leases": int(expired),
                "global_slots": self.settings.queue_global_slots,
                "max_queued": self.settings.queue_max_queued,
                "max_active_per_user": self.settings.queue_max_active_per_user,
                "oldest_queued_age_s": (self._now() - int(oldest)) if oldest else None,
                "intake_enabled": self.settings.queue_intake,
            }

    def sweep(self, now=None):
        cutoff = (now or self._now()) - self.settings.queue_retention_days * 86400
        with self.engine.begin() as connection:
            terminal_ids = connection.execute(select(jobs.c.id).where(
                jobs.c.queue_kind.is_not(None),
                jobs.c.status.in_(("succeeded", "failed", "cancelled")),
                jobs.c.updated_at < cutoff,
            )).scalars().all()
            deleted = 0
            removed_jobs = 0
            if terminal_ids:
                deleted = connection.execute(delete(queue_events).where(queue_events.c.job_id.in_(terminal_ids))).rowcount
                for job_id in terminal_ids:
                    linked = connection.execute(select(publications.c.id).where(
                        publications.c.job_id == job_id,
                    )).first()
                    if linked is None:
                        result = connection.execute(delete(jobs).where(jobs.c.id == job_id))
                        removed_jobs += int(result.rowcount or 0)
            return {"events_deleted": int(deleted or 0), "jobs_deleted": removed_jobs}

    @staticmethod
    def child_kinds():
        return frozenset({"point_forecast", "point_hindcast", "region_cell", "region_merge"})

    @staticmethod
    def parent_kinds():
        return frozenset({"region_field"})

    @staticmethod
    def top_level_kinds():
        return frozenset({"point_forecast", "point_hindcast", "region_field"})

    @staticmethod
    def accepted_kinds():
        return JobQueue.child_kinds() | JobQueue.parent_kinds() | frozenset({"noop"}) | _test_kinds()


def _test_kinds():
    import os

    return frozenset(name for name in (os.environ.get("AGROCAST_QUEUE_TEST_KINDS") or "").split(",") if name)


def _payload_size(payload):
    from agrocast.core.jsoncodec import canonical_json

    try:
        return len(canonical_json(payload).encode("utf-8"))
    except (TypeError, ValueError):
        return INLINE_REPORT_LIMIT + 1
