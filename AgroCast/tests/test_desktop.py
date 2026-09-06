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
POINT = {"lat": 45.03, "lon": 39.07, "start": "2026-10", "horizon": 3, "mode": "seasonal", "season_len": 3}


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
    assert first["payload"]["start"] == "2026-10"
    assert first["payload"]["seasons"]
    assert first["identity"]["cache_version"] == "result-v1"
    assert first["computed_at"]
    releases_path = desktop_settings.state_dir / "desktop-releases.json"
    assert releases_path.exists()
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
    from sqlalchemy import create_engine

    engine = create_engine("sqlite:///" + str(desktop_settings.state_dir / "agrocast.db"))
    with engine.connect() as connection:
        queue_rows = connection.execute(select(func.count()).select_from(jobs).where(jobs.c.queue_kind.is_not(None))).scalar_one()
        publication_rows = connection.execute(select(func.count()).select_from(publications)).scalar_one()
    engine.dispose()
    assert queue_rows == 0
    assert publication_rows == 0


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
