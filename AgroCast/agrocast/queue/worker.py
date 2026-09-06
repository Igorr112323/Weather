import argparse
import hashlib
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select

from agrocast.core.jsoncodec import strict_json
from agrocast.core.settings import ConfigurationError, RuntimeSettings
from agrocast.identity.schema import jobs
from agrocast.queue.sandbox import SandboxUnavailable, libseccomp, require_linux
from agrocast.queue.service import JobQueue, LeaseLost, QueueError
from agrocast.store.results import fingerprint

KILL_GRACE_SECONDS = 5.0
PUMP_TIMEOUT_SECONDS = 0.25
LOG_LINES_CAP = 500
POLL_INTERVAL_SECONDS = 1.0
CHILD_RESULT_DIR = "queue-staging"

log = logging.getLogger("agrocast.worker")


def new_worker_id():
    return f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:8]}"


class ComputeWorker:
    def __init__(self, settings: RuntimeSettings, engine=None, worker_id=None):
        require_linux()
        try:
            libseccomp()
        except SandboxUnavailable as error:
            raise ConfigurationError(str(error)) from None
        settings.prepare_state()
        self.settings = settings
        self.engine = engine or settings.identity_settings().engine()
        self.queue = JobQueue(self.engine, settings)
        self.worker_id = worker_id or new_worker_id()
        self.state_dir = settings.state_dir

    def child_dir(self, name):
        path = self.state_dir / name
        path.mkdir(exist_ok=True, mode=0o700)
        return path

    def _result_path(self, job_id):
        return self.child_dir(CHILD_RESULT_DIR) / (job_id + ".json")

    def _run_child(self, job):
        job_id = job["id"]
        params = dict((job["data"] or {}).get("params") or {})
        input_path = self.child_dir("queue-exec") / (job_id + ".in.json")
        exec_spec = {
            "job_id": job_id, "kind": job["queue_kind"],
            "params": {**params, "world_dir": str(self.settings.world_dir), "data_root": str(self.settings.state_dir)},
            "blas_threads": self.settings.queue_blas_threads,
            "deadline_seconds": self.settings.queue_deadline_seconds,
            "max_rss_mb": self.settings.queue_max_rss_mb,
            "result_path": str(self._result_path(job_id)),
        }
        write_text_private = json.dumps(exec_spec, ensure_ascii=False, allow_nan=False)
        input_path.write_text(write_text_private, encoding="utf-8")
        os.chmod(input_path, 0o600)
        environment = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": str(self.state_dir),
            "PYTHONHASHSEED": "0",
            "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
            "PYTHONFAULTHANDLER": "1",
            "AGROCAST_LOG_LEVEL": self.settings.log_level,
        }
        executor_module = os.environ.get("AGROCAST_QUEUE_EXECUTOR_MODULE")
        if executor_module:
            environment["AGROCAST_QUEUE_EXECUTOR_MODULE"] = executor_module
        started = time.monotonic()
        process = subprocess.Popen(
            [sys.executable, "-m", "agrocast.queue.exec", "--input", str(input_path)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            cwd=str(Path(__file__).resolve().parents[2]), env=environment, start_new_session=True,
        )
        stdout_fd = process.stdout.fileno()
        os.set_blocking(stdout_fd, False)
        buffer = b""
        log_lines = []
        result = None
        failure = None
        fatal = None
        last_poll = started
        last_beat = started
        deadline = started + self.settings.queue_deadline_seconds
        cancel_requested = False
        lease_lost = False
        eof = False
        try:
            while True:
                try:
                    chunk = os.read(stdout_fd, 65536)
                except BlockingIOError:
                    chunk = None
                if chunk is None:
                    pass
                elif chunk == b"":
                    eof = True
                else:
                    buffer += chunk
                while b"\n" in buffer:
                    raw, buffer = buffer.split(b"\n", 1)
                    text = raw.decode("utf-8", "replace").strip()
                    if not text:
                        continue
                    if not text.startswith("{"):
                        if len(log_lines) < LOG_LINES_CAP:
                            log_lines.append("exec-stderr: " + text[:500])
                        continue
                    try:
                        event = strict_json(text)
                    except (ValueError, RecursionError):
                        continue
                    if not isinstance(event, dict):
                        continue
                    kind = event.get("event")
                    if kind == "log":
                        log_lines.append(event.get("line"))
                    elif kind == "sandbox":
                        log_lines.append("sandbox applied_rules=%s verified=%s threads=%s" % (event.get("applied_rules"), event.get("verified"), json.dumps(event.get("threads"), sort_keys=True)))
                    elif kind == "result":
                        result = event
                    elif kind == "error":
                        failure = event
                    elif kind == "fatal":
                        fatal = event
                now = time.monotonic()
                if now - last_beat >= max(0.5, self.settings.queue_lease_seconds / 6):
                    try:
                        self.queue.heartbeat(job_id, self.worker_id, log_lines=log_lines)
                        log_lines = []
                        last_beat = now
                    except LeaseLost:
                        lease_lost = True
                if now - last_poll >= POLL_INTERVAL_SECONDS:
                    last_poll = now
                    try:
                        state = self.queue.current_state(job_id)
                    except Exception:
                        state = None
                    if state is None or state["lease_owner"] != self.worker_id or state["status"] != "running":
                        lease_lost = True
                    elif state["cancel_requested_at"] is not None:
                        cancel_requested = True
                if lease_lost:
                    self._terminate(process, force=True)
                    return {"outcome": "lease_lost"}
                if cancel_requested:
                    self._terminate(process, force=False)
                    self._terminate_descendants(process.pid)
                    try:
                        self.queue.finalize_cancelled(job_id, self.worker_id)
                    except LeaseLost:
                        return {"outcome": "lease_lost"}
                    return {"outcome": "cancelled"}
                if now > deadline:
                    self._terminate(process, force=True)
                    self._terminate_descendants(process.pid)
                    self._fail(job_id, "deadline_exceeded", "The compute exceeded the configured deadline", retryable=True)
                    return {"outcome": "deadline"}
                if eof and not buffer and process.poll() is not None:
                    break
            if buffer.strip():
                try:
                    event = strict_json(buffer.decode("utf-8", "replace"))
                    if isinstance(event, dict) and event.get("event") in {"log", "result", "error", "fatal"}:
                        if event["event"] == "log":
                            log_lines.append(event.get("line"))
                        elif event["event"] == "result":
                            result = event
                        elif event["event"] == "error":
                            failure = event
                        elif event["event"] == "fatal":
                            fatal = event
                except (ValueError, RecursionError):
                    pass
            process.wait(timeout=5)
            if log_lines:
                try:
                    self.queue.heartbeat(job_id, self.worker_id, log_lines=log_lines)
                except LeaseLost:
                    return {"outcome": "lease_lost"}
        finally:
            try:
                process.stdout.close()
            except (OSError, ValueError):
                pass
            input_path.unlink(missing_ok=True)
        exitcode = process.returncode
        if fatal is not None:
            self._fail(job_id, fatal.get("code") or "sandbox_unavailable", str(fatal.get("summary"))[:500], retryable=False)
            return {"outcome": "fatal"}
        if result is None:
            code = (failure or {}).get("code") or "exec_exit"
            summary = (failure or {}).get("summary") or f"compute child exited with status {exitcode}"
            retryable = failure is not None
            self._fail(job_id, code, summary, retryable=retryable)
            return {"outcome": "failed"}
        payload_path = self._result_path(job_id)
        if not payload_path.exists():
            self._fail(job_id, "result_missing", "compute child reported a result without a payload file", retryable=False)
            return {"outcome": "failed"}
        try:
            payload = strict_json(payload_path.read_bytes())
        except (ValueError, RecursionError, OSError):
            self._fail(job_id, "result_corrupt", "staged compute payload failed strict validation", retryable=False)
            return {"outcome": "failed"}
        if self._sha256_file(payload_path) != result.get("checksum"):
            payload_path.unlink(missing_ok=True)
            self._fail(job_id, "result_corrupt", "staged compute payload checksum does not match the child report", retryable=False)
            return {"outcome": "failed"}
        if self._is_top_level(job["queue_kind"]):
            outcome = self._publish_from_payload(job, payload, payload_path)
            if outcome is not None:
                return {"outcome": outcome}
            return {"outcome": "succeeded"}
        try:
            self.queue.complete(job_id, self.worker_id, payload, result_ref=CHILD_RESULT_DIR + "/" + job_id + ".json")
        except LeaseLost:
            return {"outcome": "lease_lost"}
        return {"outcome": "succeeded"}

    def _is_top_level(self, kind):
        return kind in JobQueue.top_level_kinds()

    @staticmethod
    def _sha256_file(path):
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _terminate_descendants(group_id):
        killed = 0
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            if pid == os.getpid():
                continue
            try:
                stat = (entry / "stat").read_text()
                pgid = int(stat[stat.rfind(")") + 2:].split()[2])
            except (OSError, IndexError, ValueError):
                continue
            if pgid != group_id:
                continue
            try:
                os.kill(pid, signal.SIGKILL)
                killed += 1
            except OSError:
                pass
        return killed

    def _terminate(self, process, force):
        try:
            group = os.getpgid(process.pid)
        except OSError:
            group = None
        if group is None and process.poll() is not None:
            return
        if group is not None:
            try:
                os.killpg(group, signal.SIGKILL if force else signal.SIGTERM)
            except OSError:
                pass
            if not force:
                try:
                    process.wait(timeout=KILL_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    pass
            try:
                os.killpg(group, signal.SIGKILL)
            except OSError:
                pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass

    def _fail(self, job_id, code, summary, retryable=True):
        try:
            self.queue.fail(job_id, self.worker_id, code, summary, retryable=retryable)
        except LeaseLost:
            return

    def _identity_for(self, job):
        dump = ((job["data"] or {}).get("params") or {}).get("identity")
        if dump is None:
            return None
        from agrocast.store.results import ResultIdentity

        try:
            return ResultIdentity.model_validate(dump)
        except Exception:
            return None

    def _cache_read(self, identity):
        from agrocast.store.results import ResultCache

        return ResultCache(self.settings.state_dir).read(identity)

    def _cache_write(self, identity, payload):
        from agrocast.store.results import ResultCache

        return ResultCache(self.settings.state_dir).write(identity, payload)

    def _publish_from_payload(self, job, payload, staged_path):
        identity = self._identity_for(job)
        if identity is None:
            self._fail(job["id"], "identity_unresolved", "The admitted job lost its result identity binding", retryable=False)
            return "failed"
        try:
            self._cache_write(identity, payload)
        except (ValueError, OSError):
            self._fail(job["id"], "publish_failed", "Atomic result publication failed; the payload stays staged for operator inspection", retryable=False)
            return "failed"
        result_ref = "results-v1/" + identity.key() + ".json"
        staged_path.unlink(missing_ok=True)
        try:
            self.queue.complete(job["id"], self.worker_id, payload, result_ref=result_ref)
        except LeaseLost:
            return "lease_lost"
        return None

    def _publish_from_cache(self, job):
        identity = self._identity_for(job)
        if identity is None:
            return None
        hit = self._cache_read(identity)
        if hit is None:
            return None
        try:
            self.queue.complete(job["id"], self.worker_id, hit.payload, result_ref="results-v1/" + identity.key() + ".json")
        except LeaseLost:
            return "lease_lost"
        return "published_from_cache"

    def _coordinate_region(self, job):
        job_id = job["id"]
        data = job["data"] or {}
        state = self.queue.current_state(job_id) or {}
        if state.get("cancel_requested_at") is not None:
            self.queue.finalize_cancelled(job_id, self.worker_id)
            return "cancelled"
        phase = data.get("phase")
        if phase is None:
            return self._region_expand(job)
        if phase == "cells":
            return self._region_schedule_merge(job)
        if phase == "merge":
            return self._region_finalize(job)
        self._fail(job_id, "unknown_phase", f"region coordinator phase {phase!r} is invalid", retryable=False)
        return "failed"

    def _region_grid(self, params):
        from agrocast.serve.region import grid_path

        path = grid_path(self.settings.world_dir, params["region"])
        return strict_json(path.read_text(encoding="utf-8"))

    def _region_expand(self, job):
        job_id = job["id"]
        params = job["data"]["params"]
        try:
            grid = self._region_grid(params)
        except (OSError, ValueError):
            self._fail(job_id, "grid_unavailable", "The regional grid artifact cannot be read from the bundle", retryable=False)
            return "failed"
        if fingerprint(grid) != params.get("grid_sha256"):
            self._fail(job_id, "grid_changed", "The regional grid changed after admission", retryable=True)
            return "failed"
        actor = _OwnerActor(job["owner_id"], job["organization_id"])
        child_ids = []
        for cell in grid["cells"]:
            cell_params = {
                "cell": cell, "start": params["start"], "config_snapshot": params.get("config_snapshot"),
                "dedup_seed": {"parent": job_id, "cell": cell["id"], "start": params["start"]},
            }
            queued = self.queue.enqueue(actor, "region_cell", cell_params, dedup_sha256=fingerprint(cell_params["dedup_seed"]), parent_id=job_id)
            child_ids.append(queued["job_id"])
        self.queue.park(job_id, self.worker_id, {"phase": "cells", "children": child_ids}, delay=0)
        return "expanded"

    def _region_child_payloads(self, parent_job_id):
        with self.engine.connect() as connection:
            rows = connection.execute(select(jobs).where(jobs.c.parent_id == parent_job_id)).mappings().all()
        payloads = []
        for row in rows:
            if row["queue_kind"] != "region_cell" or row["status"] != "succeeded":
                continue
            payload = (row["data"] or {}).get("report")
            if payload is None:
                ref = (row["data"] or {}).get("report_ref")
                if ref:
                    payload = strict_json((self.state_dir / ref).read_bytes())
            if payload is not None:
                payloads.append(payload)
        return payloads

    def _region_schedule_merge(self, job):
        job_id = job["id"]
        payloads = self._region_child_payloads(job_id)
        try:
            grid = self._region_grid(job["data"]["params"])
        except (OSError, ValueError):
            self._fail(job_id, "grid_unavailable", "The regional grid artifact cannot be read from the bundle", retryable=False)
            return "failed"
        merge_params = {
            "grid": grid, "cells": payloads, "start": job["data"]["params"]["start"],
            "region": job["data"]["params"]["region"], "runtime_forecast_s": 0.0,
        }
        actor = _OwnerActor(job["owner_id"], job["organization_id"])
        dedup = fingerprint({"parent": job_id, "merge": job["data"]["params"]["start"]})
        self.queue.enqueue(actor, "region_merge", merge_params, dedup_sha256=dedup, parent_id=job_id)
        self.queue.park(job_id, self.worker_id, {"phase": "merge"}, delay=0)
        return "merged_scheduled"

    def _region_finalize(self, job):
        job_id = job["id"]
        with self.engine.connect() as connection:
            child_rows = connection.execute(select(jobs).where(jobs.c.parent_id == job_id)).mappings().all()
        merge_row = next((row for row in child_rows if row["queue_kind"] == "region_merge" and row["status"] == "succeeded"), None)
        if merge_row is None:
            self._fail(job_id, "merge_missing", "No successful merge result is available for the region parent", retryable=False)
            return "failed"
        payload = (merge_row["data"] or {}).get("report")
        merge_ref = (merge_row["data"] or {}).get("report_ref")
        if payload is None and merge_ref:
            payload = strict_json((self.state_dir / merge_ref).read_bytes())
        if payload is None:
            self._fail(job_id, "merge_result_missing", "The merge child finished without a readable payload", retryable=False)
            return "failed"
        identity = self._identity_for(job)
        if identity is not None:
            from agrocast.serve.region import region_identity
            from agrocast.store.results import Releases

            try:
                releases = Releases.from_file(self.settings.release_manifest_file)
                current = region_identity(job["data"]["params"]["start"], self.settings.world_dir, job["data"]["params"]["region"], releases)
            except Exception:
                self._fail(job_id, "release_identity_unavailable", "Release manifest cannot be resolved at publish time", retryable=False)
                return "failed"
            if current != identity:
                self._fail(job_id, "result_identity_mismatch", "Regional inputs changed during computation; the result was not published", retryable=False)
                return "failed"
            try:
                self._cache_write(identity, payload)
            except (ValueError, OSError):
                self._fail(job_id, "publish_failed", "Atomic region field publication failed", retryable=False)
                return "failed"
        for row in child_rows:
            self._result_path(row["id"]).unlink(missing_ok=True)
        result_ref = "results-v1/" + identity.key() + ".json" if identity is not None else None
        try:
            self.queue.complete(job_id, self.worker_id, payload, result_ref=result_ref)
        except LeaseLost:
            return "lease_lost"
        return "succeeded"

    def tick(self):
        self.queue.reap_expired_leases()
        self.cleanup_staging()
        job = self.queue.claim(self.worker_id)
        if job is None:
            return None
        kind = job["queue_kind"]
        if kind == "region_field":
            return {"job_id": job["id"], "outcome": self._coordinate_region(job)}
        if kind in {"point_forecast", "point_hindcast"}:
            outcome = self._publish_from_cache(job)
            if outcome in {"lease_lost", "published_from_cache"}:
                return {"job_id": job["id"], "outcome": outcome}
        return {"job_id": job["id"], **self._run_child(job)}

    def cleanup_staging(self):
        directory = self.child_dir(CHILD_RESULT_DIR)
        child = jobs.alias()
        parent = jobs.alias()
        with self.engine.connect() as connection:
            active = set(connection.execute(select(jobs.c.id).where(
                jobs.c.queue_kind.is_not(None), jobs.c.status.in_(("queued", "running")),
            )).scalars().all())
            succeeded_with_parent = set(connection.execute(
                select(child.c.id).select_from(child.join(parent, child.c.parent_id == parent.c.id)).where(
                    child.c.queue_kind.is_not(None), child.c.status == "succeeded",
                    parent.c.status.in_(("queued", "running")),
                )).scalars().all())
        keep = active | succeeded_with_parent
        removed = 0
        for path in directory.glob("*.json"):
            owner_id = path.name.split(".", 1)[0]
            if owner_id in keep:
                continue
            path.unlink(missing_ok=True)
            removed += 1
        exec_directory = self.child_dir("queue-exec")
        for path in exec_directory.glob("*"):
            owner_id = path.name.split(".", 1)[0]
            if owner_id in keep:
                continue
            path.unlink(missing_ok=True)
            removed += 1
        return removed

    def run(self, max_jobs=None, idle_seconds=0.5, stop_file=None):
        processed = 0
        while max_jobs is None or processed < max_jobs:
            result = self.tick()
            if result is None:
                if stop_file and Path(stop_file).exists():
                    break
                time.sleep(idle_seconds)
                continue
            processed += 1
        return processed


class _OwnerActor:
    def __init__(self, owner_id, organization_id):
        self.id = owner_id
        self.organization_id = organization_id
        self.role = None
        self.session_hash = ""
        self.csrf_token = ""
        self.username = "queue-worker"

    def public(self):
        return {"username": self.username, "role": "operator"}


def _configure_logging(settings):
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s :: %(message)s"))
    root = logging.getLogger("agrocast.worker")
    root.handlers = [handler]
    root.setLevel(settings.log_level)
    root.propagate = False


def main(argv=None):
    parser = argparse.ArgumentParser(prog="agrocast-queue-worker")
    parser.add_argument("command", choices=["run", "once", "sweep", "stats"], nargs="?", default="run")
    parser.add_argument("--max-jobs", type=int, default=None)
    parser.add_argument("--stop-file", default=None)
    parser.add_argument("--state-dir", "--data-dir", dest="data_dir", default=None)
    parser.add_argument("--world-dir", default=None)
    args = parser.parse_args(argv)
    try:
        settings = RuntimeSettings.from_environment().with_paths(args.world_dir, args.data_dir)
        worker = ComputeWorker(settings)
    except (ConfigurationError, QueueError) as error:
        print(str(error), file=sys.stderr)
        return 78
    _configure_logging(settings)
    if args.command == "once":
        result = worker.tick()
        print(json.dumps({"worker": worker.worker_id, "result": result}, ensure_ascii=False, default=str))
        return 0
    if args.command == "sweep":
        removed = worker.cleanup_staging()
        events = worker.queue.sweep()
        print(json.dumps({"staging_removed": removed, **events}))
        return 0
    if args.command == "stats":
        print(json.dumps(worker.queue.stats(None), ensure_ascii=False))
        return 0
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        worker.run(max_jobs=args.max_jobs, stop_file=args.stop_file)
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
