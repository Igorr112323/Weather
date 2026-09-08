import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from agrocast.core.settings import RuntimeSettings
from agrocast.identity.schema import jobs, publications, users
from agrocast.serve.product import create_app
from agrocast.serve.runtime import LOCAL_ORG_ID, LOCAL_USER_ID

ORIGIN = "https://127.0.0.1"
POINT = {"lat": 45.03, "lon": 39.07, "start": "2026-03", "horizon": 3, "mode": "seasonal", "season_len": 3}


@pytest.fixture
def desktop_settings(tmp_path):
    return RuntimeSettings(world_dir=Path(__file__).resolve().parents[1] / "world", state_dir=tmp_path / "state", public_origin=ORIGIN, desktop_mode=True)


@pytest.fixture
def desktop_client(desktop_settings):
    application = create_app(settings=desktop_settings)
    with TestClient(application, base_url=ORIGIN) as client:
        yield client


def test_local_owner_is_provisioned_once(desktop_client, desktop_settings):
    database = desktop_settings.state_dir / "agrocast.db"
    assert database.exists()
    from sqlalchemy import create_engine

    engine = create_engine("sqlite:///" + str(database))
    with engine.connect() as connection:
        rows = connection.execute(select(users.c.id, users.c.username, users.c.role, users.c.organization_id)).mappings().all()
    engine.dispose()
    assert len(rows) == 1
    assert rows[0].id == LOCAL_USER_ID
    assert rows[0].organization_id == LOCAL_ORG_ID
    assert rows[0].role == "admin"


def test_windowed_stdio_guard_provides_streams():
    # PyInstaller console=False sets sys.stdout/sys.stderr to None. The desktop
    # entry point must substitute a real stream before uvicorn configures logging.
    import sys

    from agrocast.desktop.app import _ensure_stdio

    original_out, original_err = sys.stdout, sys.stderr
    try:
        sys.stdout = None
        sys.stderr = None
        _ensure_stdio()
        assert sys.stdout is not None and hasattr(sys.stdout, "write")
        assert sys.stderr is not None and hasattr(sys.stderr, "write")
        assert not isinstance(sys.stdout, type(None))
        assert not isinstance(sys.stderr, type(None))
    finally:
        sys.stdout, sys.stderr = original_out, original_err


def test_bundle_integrity_accepts_valid_marker(tmp_path):
    import hashlib
    import json

    from agrocast.desktop.app import _verify_bundle_integrity

    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "c.txt").write_text("world", encoding="utf-8")

    digest = hashlib.sha256()
    for entry in sorted(tmp_path.rglob("*")):
        if entry.is_file() and entry.name != "integrity.json":
            digest.update(entry.relative_to(tmp_path).as_posix().encode())
            digest.update(entry.read_bytes())
    (tmp_path / "integrity.json").write_text(
        json.dumps({"sha256_prefix": digest.hexdigest()[:16]}), encoding="utf-8"
    )
    _verify_bundle_integrity(tmp_path)  # must not raise (regression: json import)

    (tmp_path / "a.txt").write_text("tampered", encoding="utf-8")
    with pytest.raises(RuntimeError):
        _verify_bundle_integrity(tmp_path)


def test_bundle_integrity_is_line_ending_independent(tmp_path):
    # A Windows checkout may normalise text files to CRLF; the integrity check
    # must still pass because it folds line endings back to LF for text files.
    import hashlib
    import json

    from agrocast.desktop.app import _verify_bundle_integrity

    (tmp_path / "b").mkdir()
    (tmp_path / "a.txt").write_bytes(b"hello\n")
    (tmp_path / "b" / "c.txt").write_bytes(b"world\n")
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02")

    digest = hashlib.sha256()
    for entry in sorted(tmp_path.rglob("*")):
        if entry.is_file() and entry.name != "integrity.json":
            digest.update(entry.relative_to(tmp_path).as_posix().encode())
            digest.update(entry.read_bytes())
    (tmp_path / "integrity.json").write_text(
        json.dumps({"sha256_prefix": digest.hexdigest()[:16]}), encoding="utf-8"
    )

    # Re-write the text files with CRLF line endings (simulating Windows checkout).
    for name in ("a.txt", "b/c.txt"):
        p = tmp_path / name
        p.write_bytes(p.read_bytes().replace(b"\n", b"\r\n"))

    _verify_bundle_integrity(tmp_path)  # must not raise even though text is CRLF


def test_bundle_integrity_skips_without_marker(tmp_path):
    from agrocast.desktop.app import _verify_bundle_integrity

    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    _verify_bundle_integrity(tmp_path)  # no integrity.json -> no-op


def test_checked_in_world_bundle_integrity_is_valid():
    from agrocast.desktop.app import _verify_bundle_integrity

    _verify_bundle_integrity(Path(__file__).resolve().parents[1] / "world")


def test_bundle_integrity_os_independent_with_crlf(tmp_path):
    """Regression: CRLF in text files or backslash paths must not change the hash.

    On Windows, git may check out text files with CRLF and pathlib produces
    backslash paths.  Both must be normalised so the sha256 matches the
    Linux-generated integrity.json.
    """
    import hashlib

    from agrocast.desktop.app import _bundle_content_bytes, _verify_bundle_integrity

    (tmp_path / "a.txt").write_bytes(b"hello\nworld\n")
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "c.txt").write_bytes(b"data\n")

    # Compute the expected hash the same way the code does (posix paths + LF normalised).
    digest = hashlib.sha256()
    for entry in sorted(tmp_path.rglob("*")):
        if entry.is_file() and entry.name != "integrity.json":
            digest.update(entry.relative_to(tmp_path).as_posix().encode())
            digest.update(_bundle_content_bytes(entry))
    (tmp_path / "integrity.json").write_text(
        json.dumps({"sha256_prefix": digest.hexdigest()[:16]}), encoding="utf-8"
    )

    # Rewrite text files with CRLF (simulates Windows checkout)
    for p in tmp_path.rglob("*.txt"):
        p.write_bytes(p.read_bytes().replace(b"\n", b"\r\n"))

    _verify_bundle_integrity(tmp_path)  # must still pass


def test_desktop_icon_is_bundled_linked_and_served(desktop_client):
    from agrocast.serve import browser_policy
    assert (browser_policy.STATIC / "agrocast.png").exists()
    assert "agrocast.png" in browser_policy.ASSET_NAMES
    page = desktop_client.get("/")
    assert '<link rel="icon" type="image/png" href="/assets/agrocast.png">' in page.text
    icon = desktop_client.get("/assets/agrocast.png")
    assert icon.status_code == 200
    assert icon.headers["content-type"].startswith("image/png")


def test_desktop_serves_app_page_without_login(desktop_client):
    response = desktop_client.get("/")
    assert response.status_code == 200
    assert "локальные прогнозы" in response.text
    assert "Показать прогноз" in response.text


def test_capabilities_marks_local_mode(desktop_client):
    payload = desktop_client.get("/api/capabilities").json()
    assert payload["operations"]["local_mode"] is True
    assert payload["operations"]["queue_intake"] is False


def test_web_auth_and_queue_admission_are_disabled_on_desktop(desktop_client):
    response = desktop_client.post("/api/auth/login", json={"username": "x", "password": "y"})
    assert response.status_code == 403
    assert response.json()["code"] == "desktop_operation_disabled"
    response = desktop_client.post("/api/prepare", json=POINT)
    assert response.status_code == 403
    assert response.json()["code"] == "desktop_operation_disabled"
    response = desktop_client.get("/api/admin/users")
    assert response.status_code == 403


def test_desktop_input_listing_reports_bundle_and_cache(desktop_client, desktop_settings):
    response = desktop_client.get("/api/local/inputs")
    assert response.status_code == 200
    body = response.json()
    assert body["bundle"]["file_count"] > 100
    assert any(entry["path"] == "ready.json" for entry in body["bundle"]["files"])
    assert body["releases"] is None
    assert body["results_cache"]["total"] == 0
    assert body["state_dir"] == str(desktop_settings.state_dir)
    assert isinstance(body["bundle_manifest"]["configuration"], dict)
    response = desktop_client.get("/api/local/inputs?x=1&x=2")
    assert response.status_code == 422


def test_local_forecast_computes_caches_and_skips_queue_tables(desktop_client, desktop_settings):
    response = desktop_client.post("/api/local/forecast", json=POINT)
    assert response.status_code == 200, response.text
    first = response.json()
    assert first["cached"] is False
    assert first["payload"]["start"] == "2026-03"
    assert first["payload"]["seasons"]
    assert first["identity"]["cache_version"] == "result-v1"
    assert first["computed_at"]
    releases_path = desktop_settings.state_dir / "desktop-releases.json"
    assert releases_path.exists()
    if os.name == "posix":
        assert oct(os.stat(releases_path).st_mode)[-3:] == "600"
    manifest = json.loads(releases_path.read_text())
    for key in ("data_release", "model_release", "application_release"):
        assert len(manifest[key]) == 64
    second = desktop_client.post("/api/local/forecast", json=POINT).json()
    assert second["cached"] is True
    assert second["payload"] == first["payload"]
    cache_root = desktop_settings.state_dir / "results-v1"
    assert len(list(cache_root.glob("*.json"))) == 1
    body = desktop_client.get("/api/local/inputs").json()
    assert body["results_cache"]["total"] == 1
    assert body["releases"] is not None
    import re

    for name in ("daily_region", "fields_monthly", "sst", "strat_snow", "oisst_boxes", "regimes"):
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", body["sources_through"][name])
    stale = desktop_client.post("/api/local/forecast", json={**POINT, "start": "2026-10"})
    assert stale.status_code == 422
    assert stale.json()["code"] == "issue_inputs_mismatch"
    assert desktop_client.get("/health/ready").status_code in (200, 503)
    from sqlalchemy import create_engine

    engine = create_engine("sqlite:///" + str(desktop_settings.state_dir / "agrocast.db"))
    with engine.connect() as connection:
        queue_rows = connection.execute(select(func.count()).select_from(jobs).where(jobs.c.queue_kind.is_not(None))).scalar_one()
        publication_rows = connection.execute(select(func.count()).select_from(publications)).scalar_one()
    engine.dispose()
    assert queue_rows == 0
    assert publication_rows == 0


def test_local_forecast_error_returns_real_reason(desktop_client, monkeypatch):
    # Пользователь должен видеть конкретную причину, а не просто «compute_failed».
    def broken(*args, **kwargs):
        raise RuntimeError("PointDataset: zarr group not found")

    monkeypatch.setattr("agrocast.forecast.orchestrator.forecast_point", broken)
    response = desktop_client.post("/api/local/forecast", json=POINT)
    assert response.status_code == 500
    body = response.json()
    assert body["code"] == "compute_failed"
    assert "PointDataset: zarr group not found" in body["error"]
    assert "RuntimeError" in body["error"]


def test_local_hindcast_error_returns_real_reason(desktop_client, monkeypatch):
    def broken(*args, **kwargs):
        raise ValueError("для этой даты нет записей проверки")

    monkeypatch.setattr("agrocast.serve.pipeline.run_hindcast", broken)
    response = desktop_client.post("/api/local/hindcast", json={**POINT, "start": "2020-06"})
    assert response.status_code == 500
    body = response.json()
    assert body["code"] == "compute_failed"
    assert "для этой даты нет записей проверки" in body["error"]
    assert "ValueError" in body["error"]


def test_local_forecast_validates_shape_region_and_content_type(desktop_client):
    response = desktop_client.post("/api/local/forecast", json={"lat": 10.0, "lon": 10.0, "start": "2026-10"})
    assert response.status_code == 422
    assert response.json()["code"] in {"point_outside_region", "invalid_request"}
    response = desktop_client.post("/api/local/forecast", json={"lat": 45.03, "lon": 39.07, "start": "bad"})
    assert response.status_code == 422
    response = desktop_client.post("/api/local/forecast", data="{}", headers={"content-type": "text/plain"})
    assert response.status_code == 415


def test_local_endpoints_are_closed_outside_desktop(identity, tmp_path):
    settings = RuntimeSettings(world_dir=Path(__file__).resolve().parents[1] / "world", state_dir=tmp_path / "web-state", public_origin="https://testserver")
    application = create_app(identity=identity, settings=settings)
    client = TestClient(application, base_url="https://testserver", headers={"Origin": "https://testserver"})
    with client as session:
        assert session.post("/api/local/forecast", json=POINT).status_code == 401
        assert session.get("/api/local/inputs").status_code == 401
        assert session.get("/desktop.html").status_code == 401
