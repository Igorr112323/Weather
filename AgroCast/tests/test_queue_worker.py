import json
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text

from agrocast.core.contracts import ForecastSpec
from agrocast.core.settings import RuntimeSettings
from agrocast.identity.database import migrate
from agrocast.identity.schema import jobs, publications
from agrocast.queue.admission import admit_point
from agrocast.queue.sandbox import SandboxUnavailable, libseccomp
from agrocast.queue.service import JobQueue
from agrocast.store.results import ResultCache

from test_queue_core import make_principal

ORIGIN = "https://testserver"
AGRO_DIR = Path(__file__).resolve().parents[1]
DIGESTS = {"data_release": "a" * 64, "model_release": "b" * 64, "application_release": "c" * 64}
READY_POINT = {"lat": 45.03, "lon": 39.07, "start": "2026-03", "horizon": 3, "mode": "seasonal", "season_len": 3}
TEST_KINDS = "sleepy,grandchild,probe,boom"

try:
    libseccomp()
    _SANDBOX_READY = sys.platform.startswith("linux")
except (SandboxUnavailable, OSError):
    _SANDBOX_READY = False

pytestmark = pytest.mark.skipif(
    not _SANDBOX_READY, reason="the durable compute worker requires Linux with libseccomp2"
)


def row_state(engine, job_id):
    with engine.connect() as connection:
        row = connection.execute(select(jobs).where(jobs.c.id == job_id)).mappings().first()
    return dict(row) if row is not None else None


def wait_for(predicate, timeout=30.0, interval=0.25):
    limit = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() >= limit:
            return None


def alive(pid):
    return Path("/proc/" + str(pid)).exists()


class WorkerHarness:
    def __init__(self, engine, settings, queue, principal, tmp_path, dsn_file, state_dir, releases_path):
        self.engine = engine
        self.settings = settings
        self.queue = queue
        self.principal = principal
        self.tmp = tmp_path
        self.dsn_file = dsn_file
        self.state_dir = state_dir
        self.releases_path = releases_path
        self.processes = []
        self.counter = 0

    def marker(self, name):
        self.counter += 1
        return self.tmp / ("%s-%d.json" % (name, self.counter))

    def enqueue(self, kind, params):
        result = self.queue.enqueue(self.principal, kind, params)
        return result["job_id"]

    def environment(self, **overrides):
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": str(self.tmp),
            "PYTHONHASHSEED": "0",
            "PYTHONPATH": os.pathsep.join([str(AGRO_DIR), str(Path(__file__).parent)]),
            "AGROCAST_STATE_DIR": str(self.state_dir),
            "AGROCAST_WORLD_DIR": str(AGRO_DIR / "world"),
            "AGROCAST_PUBLIC_ORIGIN": ORIGIN,
            "AGROCAST_DATABASE_URL_FILE": str(self.dsn_file),
            "AGROCAST_RELEASE_MANIFEST_FILE": str(self.releases_path),
            "AGROCAST_QUEUE_INTAKE": "1",
            "AGROCAST_QUEUE_LEASE_SECONDS": "10",
            "AGROCAST_QUEUE_RETRY_SECONDS": "1",
            "AGROCAST_QUEUE_RETRY_CAP_SECONDS": "10",
            "AGROCAST_QUEUE_MAX_ATTEMPTS": "5",
            "AGROCAST_QUEUE_DEADLINE_SECONDS": "300",
            "AGROCAST_QUEUE_TEST_KINDS": TEST_KINDS,
            "AGROCAST_QUEUE_EXECUTOR_MODULE": "queue_test_executor",
            "OMP_NUM_THREADS": "4",
            "OPENBLAS_NUM_THREADS": "4",
            "MKL_NUM_THREADS": "4",
        }
        env.update(overrides)
        return env

    def cli(self, *arguments, timeout=120.0, **overrides):
        log_path = self.tmp / ("cli-%s.log" % uuid4().hex[:8])
        environment = self.environment(**overrides)
        started = subprocess.run(
            [sys.executable, "-m", "agrocast.queue.worker", *arguments],
            cwd=str(AGRO_DIR), env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, timeout=timeout,
        )
        log_path.write_text(started.stdout, encoding="utf-8")
        assert started.returncode == 0, started.stdout
        return started.stdout

    def once(self, **overrides):
        outcome = self.cli("once", **overrides)
        return json.loads(outcome.strip().splitlines()[-1])

    def spawn(self, *arguments, **overrides):
        log = open(self.tmp / "worker.log", "wb")
        process = subprocess.Popen(
            [sys.executable, "-m", "agrocast.queue.worker", *arguments],
            cwd=str(AGRO_DIR), env=self.environment(**overrides), stdout=log, stderr=subprocess.STDOUT,
        )
        self.processes.append((process, log))
        return process

    def staging(self, job_id):
        return self.state_dir / "queue-staging" / (job_id + ".json")


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setenv("AGROCAST_QUEUE_TEST_KINDS", TEST_KINDS)
    configured = os.environ.get("AGROCAST_TEST_DATABASE_URL_FILE")
    server = None
    if configured:
        url = Path(configured).read_text(encoding="utf-8").strip()
    else:
        pgserver = pytest.importorskip("pgserver")
        server = pgserver.get_server(tmp_path / "pgdata", cleanup_mode="stop")
        server.ensure_pgdata_inited()
        server.ensure_postgres_running()
        url = server.get_uri()
    if "+psycopg" not in url:
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    engine = create_engine(url, hide_parameters=True, future=True)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    migrate(engine)
    principal = make_principal(engine)
    dsn_file = tmp_path / "database_url"
    dsn_file.write_text(url, encoding="utf-8")
    os.chmod(dsn_file, 0o600)
    releases_path = tmp_path / "releases.json"
    releases_path.write_text(json.dumps(DIGESTS), encoding="utf-8")
    state_dir = tmp_path / "state"
    settings = RuntimeSettings(
        state_dir=state_dir, public_origin=ORIGIN, release_manifest_file=releases_path,
        database_url_file=dsn_file, queue_intake=True, queue_lease_seconds=10,
        queue_retry_seconds=1, queue_retry_cap_seconds=10, queue_max_attempts=5,
    )
    queue = JobQueue(engine, settings)
    harness = WorkerHarness(engine, settings, queue, principal, tmp_path, dsn_file, state_dir, releases_path)
    harness.server = server
    try:
        yield harness
    finally:
        for process, log in harness.processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            log.close()
        engine.dispose()
        if server is not None:
            server.cleanup()


def test_worker_executes_point_forecast_and_publishes_once(harness):
    admitted = admit_point(
        harness.queue, None, harness.settings, harness.principal, ForecastSpec.model_validate(READY_POINT)
    )
    job_id = admitted["job_id"]
    assert row_state(harness.engine, job_id)["status"] == "queued"
    outcome = harness.once(timeout=420.0)
    assert outcome["result"]["outcome"] == "succeeded"
    row = row_state(harness.engine, job_id)
    assert row["status"] == "succeeded"
    assert row["attempts"] == 1
    assert row["lease_owner"] is None and row["lease_expires_at"] is None
    assert row["result_checksum"] and row["data"]["report"]["seasons"]
    with harness.engine.connect() as connection:
        published = connection.execute(select(publications).where(publications.c.job_id == job_id)).mappings().all()
    assert len(published) == 1
    assert published[0].data["result_checksum"] == row["result_checksum"]
    cached = ResultCache(harness.state_dir).read(_identity_from(row["data"]["params"]["identity"]))
    assert cached is not None and cached.payload == row["data"]["report"]
    rerun = harness.queue.enqueue(harness.principal, "point_forecast", row["data"]["params"], dedup_sha256=row["dedup_sha256"])
    assert rerun["job_id"] != job_id
    outcome = harness.once()
    assert outcome["result"]["outcome"] == "published_from_cache"
    second = row_state(harness.engine, rerun["job_id"])
    assert second["status"] == "succeeded"
    assert second["result_checksum"] == row["result_checksum"]
    with harness.engine.connect() as connection:
        total_points = connection.execute(select(jobs.c.id).where(jobs.c.queue_kind == "point_forecast")).scalars().all()
    assert len(total_points) == 2


def _identity_from(dump):
    from agrocast.store.results import ResultIdentity

    return ResultIdentity.model_validate(dump)


def test_worker_restart_keeps_job_identity_and_requeues(harness):
    marker = harness.marker("restart-pid")
    job_id = harness.enqueue("sleepy", {"seconds": 4, "pid_file": str(marker)})
    assert row_state(harness.engine, job_id)["status"] == "queued"
    worker = harness.spawn("run", "--max-jobs", "1")
    child = wait_for(lambda: json.loads(marker.read_text()) if marker.exists() else None, timeout=25)
    assert child and alive(child["pid"])
    while row_state(harness.engine, job_id)["status"] != "running":
        time.sleep(0.2)
    worker.kill()
    assert worker.wait(timeout=15) < 0
    row = row_state(harness.engine, job_id)
    assert row["status"] == "running"
    assert row["lease_owner"]
    time.sleep(11)
    for _ in range(3):
        with harness.engine.begin() as connection:
            connection.execute(jobs.update().where(jobs.c.id == job_id).values(next_retry_at=None))
        if harness.once()["result"] is not None:
            break
    row = row_state(harness.engine, job_id)
    assert row["status"] == "succeeded"
    assert row["attempts"] == 2
    assert row["data"]["report"]["slept"] == 4.0
    assert harness.staging(job_id).exists()
    output = json.loads(harness.cli("sweep").strip().splitlines()[-1])
    assert output["staging_removed"] >= 1
    assert not harness.staging(job_id).exists()


def _if(row, condition):
    return row if condition else None


def test_cancel_running_job_kills_the_whole_process_group(harness):
    marker = harness.marker("cancel-pid")
    child_marker = harness.marker("cancel-grandchild")
    job_id = harness.enqueue("grandchild", {
        "seconds": 60, "child_seconds": 40,
        "pid_file": str(marker), "child_pid_file": str(child_marker),
    })
    worker = harness.spawn("run", "--max-jobs", "1")
    pid = wait_for(lambda: json.loads(marker.read_text())["pid"] if marker.exists() else None, timeout=25)
    assert pid and alive(pid)
    wait_for(lambda: _if(row_state(harness.engine, job_id), row_state(harness.engine, job_id)["status"] == "running"), timeout=10)
    canceled = harness.queue.cancel(harness.principal, job_id, "operator test")
    assert canceled["status"] == "running" and canceled["cancel_requested"] is True
    final = wait_for(lambda: _if(row_state(harness.engine, job_id), row_state(harness.engine, job_id)["status"] == "cancelled"), timeout=25)
    assert final, "worker log:\n" + (harness.tmp / "worker.log").read_text(errors="replace")[-3000:]
    row = row_state(harness.engine, job_id)
    assert row["lease_owner"] is None and row["lease_expires_at"] is None
    assert not row["data"].get("report")
    gone = wait_for(lambda: not alive(pid) and not alive(int(child_marker.read_text())), timeout=10)
    assert gone, "child processes survived the cancellation"
    assert worker.wait(timeout=30) == 0


def test_child_runs_in_the_hardened_sandbox_with_single_blas_thread(harness):
    job_id = harness.enqueue("probe", {})
    outcome = harness.once()
    row = row_state(harness.engine, job_id)
    assert outcome["result"]["outcome"] == "succeeded", (row["data"].get("error_code"), row["data"].get("error_summary"), (row["data"].get("log") or []))
    report = row["data"]["report"]
    assert report["omp"] == "1"
    assert report["openblas"] == "1"
    assert report["process"]["NoNewPrivs"] == "1"
    assert report["process"]["Seccomp"] == "2"
    assert report["ptrace_blocked"] is True
    assert report["rlimit_cpu"][0] > 0
    assert report["rlimit_nofile"][0] <= 4096
    assert row["data"]["log"]
    assert any("sandbox" in line for line in row["data"]["log"])


def test_handler_failure_retries_with_backoff_until_exhausted(harness):
    job_id = harness.enqueue("boom", {})
    outcome = harness.once()
    assert outcome["result"]["outcome"] == "failed"
    row = row_state(harness.engine, job_id)
    assert row["status"] == "queued"
    assert row["attempts"] == 1
    assert row["data"]["error_code"] == "compute_failed"
    assert row["next_retry_at"] is not None
    with harness.engine.begin() as connection:
        connection.execute(jobs.update().where(jobs.c.id == job_id).values(next_retry_at=None))
    outcome = harness.once()
    assert outcome["result"]["outcome"] == "failed"
    for _ in range(6):
        with harness.engine.begin() as connection:
            connection.execute(jobs.update().where(jobs.c.id == job_id).values(next_retry_at=None))
        if row_state(harness.engine, job_id)["status"] == "failed":
            break
        harness.once()
    row = row_state(harness.engine, job_id)
    assert row["status"] == "failed"
    assert row["attempts"] == harness.settings.queue_max_attempts


def test_deadline_terminates_the_child_and_requeues_the_job(harness):
    marker = harness.marker("deadline-pid")
    job_id = harness.enqueue("sleepy", {"seconds": 60, "pid_file": str(marker)})
    outcome = harness.once(AGROCAST_QUEUE_DEADLINE_SECONDS="5")
    assert outcome["result"]["outcome"] == "deadline"
    row = row_state(harness.engine, job_id)
    assert row["status"] == "queued"
    assert row["data"]["error_code"] == "deadline_exceeded"
    child = json.loads(marker.read_text())["pid"]
    gone = wait_for(lambda: not alive(child), timeout=15)
    assert gone


def test_sweep_cli_removes_stale_staging_and_stats_reports_depth(harness):
    stale = harness.state_dir / "queue-staging"
    stale.mkdir(parents=True, exist_ok=True)
    (stale / (str(uuid4()) + ".json")).write_text("{}", encoding="utf-8")
    (stale / "broken.json").write_text("{}\n{}\n", encoding="utf-8")
    output = json.loads(harness.cli("sweep").strip().splitlines()[-1])
    assert output["staging_removed"] >= 2
    assert list(stale.glob("*.json")) == []
    harness.enqueue("sleepy", {"seconds": 0})
    stats = json.loads(harness.cli("stats").strip().splitlines()[-1])
    assert stats["queued"] >= 1
    assert stats["max_queued"] == harness.settings.queue_max_queued
    assert stats["intake_enabled"] is True
