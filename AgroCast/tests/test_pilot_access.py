from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from agrocast.serve import product
from agrocast.serve.pilot import PILOT_WARNING, pilot_capabilities, pilot_points
from agrocast.serve.security import AccessGuard


@pytest.mark.parametrize("path", ["/api/fields", "/api/crops", "/api/subscriptions", "/api/jobs", "/api/auth/me", "/api/health", "/api/region/grid", "/api/value", "/docs", "/openapi.json"])
def test_api_reads_require_individual_authentication(anonymous, path):
    response = anonymous.get(path)
    assert response.status_code == 401
    assert response.json()["code"] == "auth_required"


@pytest.mark.parametrize("method,path", [("POST", "/api/prepare"), ("POST", "/api/region/refresh"), ("POST", "/api/crops"), ("DELETE", "/api/crops/test"), ("POST", "/api/subscribe"), ("POST", "/api/fields")])
def test_anonymous_operations_fail_before_body_parsing(anonymous, method, path):
    response = anonymous.request(method, path, content=b"invalid json", headers={"Content-Type": "application/json"})
    assert response.status_code == 401


@pytest.mark.parametrize("username", ["reader_a", "operator_a", "admin_a"])
@pytest.mark.parametrize("method,path", [("POST", "/api/prepare"), ("POST", "/api/region/refresh"), ("POST", "/api/subscribe"), ("GET", "/api/ledger"), ("GET", "/api/region/field"), ("GET", "/docs"), ("GET", "/openapi.json"), ("POST", "/api/capabilities"), ("POST", "/health/live"), ("POST", "/api/jobs")])
def test_scientific_exclusions_apply_to_all_roles_without_side_effects(clients, monkeypatch, username, method, path, identity_engine):
    from sqlalchemy import func, select

    from agrocast.identity.schema import jobs

    admit = Mock(side_effect=AssertionError("no compute admission"))
    region = Mock(side_effect=AssertionError("no region worker"))
    monkeypatch.setattr(product, "admit_point", admit)
    monkeypatch.setattr(product, "admit_region", admit)
    from agrocast.serve import region as region_module
    monkeypatch.setattr(region_module, "build_field", region)

    def queue_rows():
        with identity_engine.connect() as connection:
            return connection.execute(select(func.count()).select_from(jobs).where(jobs.c.queue_kind.is_not(None))).scalar_one()

    before = queue_rows()
    payload = {}
    if path == "/api/prepare":
        payload = {"lat": 46.25, "lon": 38.25, "start": "2026-10"}
    elif path == "/api/region/refresh":
        payload = {"start": "2026-10"}
    response = clients(username).request(method, path, json=payload)
    assert response.status_code == 403
    expected = "role_forbidden" if username == "reader_a" and path in {"/api/prepare", "/api/region/refresh"} else "pilot_operation_disabled"
    assert response.json()["code"] == expected
    admit.assert_not_called()
    region.assert_not_called()
    assert queue_rows() == before


@pytest.mark.parametrize("path", ["/api/prepare/", "/api%2Fprepare", "/api/region/refresh/", "/api/crops%2Ftest"])
def test_encoded_paths_cannot_bypass_policy(clients, path):
    assert clients().post(path, json={}).status_code in (403, 422)


@pytest.mark.parametrize("region", ["rostov", "stavropol", "", "../krai", "other"])
@pytest.mark.parametrize("path", ["grid", "skill", "regions"])
def test_regions_outside_pilot_are_rejected(clients, path, region):
    response = clients().get("/api/region/" + path, params={"region": region})
    assert response.status_code == 403
    assert response.json()["code"] == "pilot_region_disabled"


def test_repeated_region_parameters_cannot_expand_scope(clients):
    client = clients()
    for query in ("region=rostov&region=krai", "region=krai&region=rostov"):
        assert client.get("/api/region/grid?" + query).status_code == 403


@pytest.mark.parametrize("path", ["/api/region/grid", "/api/region/skill", "/api/region/regions", "/api/value"])
def test_historical_results_remain_unverified(clients, path):
    response = clients().get(path)
    assert response.status_code == 200
    validation = response.json()["validation"]
    assert validation["status"] == "unverified"
    for key in ("production_ready", "agronomic_use_allowed", "coverage_guaranteed"):
        assert validation[key] is False
    assert validation["warning"] == PILOT_WARNING


def test_capabilities_distinguish_account_management_from_forecast_permission(anonymous):
    payload = anonymous.get("/api/capabilities").json()
    assert payload["stage"] == "closed_pilot"
    assert payload["access"]["authentication"] == "server_session"
    assert payload["access"]["roles"] == ["reader", "operator", "admin"]
    assert payload["access"]["ownership_required"] is True
    assert payload["forecast"] == {"enabled": False, "modes": [], "horizons": [], "season_lengths": []}
    for key in ("hindcast", "region_refresh", "subscriptions", "agro_recommendations", "legacy_api"):
        assert payload["operations"][key] is False
    assert len(payload["regions"][0]["inspection_points"]) == 28
    assert [row["id"] for row in payload["crops"]] == ["maize"]


def test_policy_payloads_are_not_mutable_shared_state():
    payload = pilot_capabilities()
    payload["regions"].clear()
    payload["forecast"]["enabled"] = True
    assert pilot_capabilities()["regions"][0]["id"] == "krai"
    assert pilot_capabilities()["forecast"]["enabled"] is False


def test_only_frozen_pilot_grid_and_region_are_available(clients):
    client = clients()
    grid = client.get("/api/region/grid").json()["grid"]
    assert grid["n_cells"] == 28
    assert [{key: cell[key] for key in ("id", "lat", "lon")} for cell in grid["cells"]] == pilot_points()
    assert [row["region"] for row in client.get("/api/region/regions").json()["regions"]] == ["krai"]


def test_legacy_process_local_jobs_are_not_implicitly_assigned(clients):
    assert not hasattr(product, "JOBS")
    response = clients("admin_a").get("/api/job/" + str(uuid4()))
    assert response.status_code == 404


def test_html_redirects_to_login_and_warns_without_javascript(anonymous, clients):
    response = anonymous.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    client = clients()
    assert client.get("/").status_code == 200
    response = client.get("/value.html")
    assert PILOT_WARNING in response.text
    assert "{{PILOT_WARNING}}" not in response.text
    assert "Навык подтверждён только для температуры" not in response.text


@pytest.mark.parametrize("path", ["/health/live", "/api/capabilities", "/api/value", "/api/prepare", "/login"])
def test_responses_are_not_cacheable_or_indexable(anonymous, path):
    response = anonymous.get(path)
    assert response.headers["cache-control"] == "no-store"
    assert "Cookie" in response.headers["vary"]
    assert response.headers["x-robots-tag"] == "noindex, nofollow"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_unaudited_future_route_is_denied(application, clients):
    called = []

    @application.get("/api/new-expensive-read")
    def new_route():
        called.append(True)
        return {"ok": True}

    assert clients().get("/api/new-expensive-read").status_code == 403
    assert not called


def test_mount_prefix_cannot_bypass_protection(identity):
    parent = FastAPI()
    parent.mount("/pilot", product.create_app(identity))
    with TestClient(parent, base_url="https://testserver") as client:
        assert client.get("/pilot/api/fields").status_code == 401
        assert client.post("/pilot/api/prepare", json={}).status_code == 401


def test_websocket_access_is_closed(anonymous):
    with pytest.raises(WebSocketDisconnect) as error:
        with anonymous.websocket_connect("/api/jobs"):
            pass
    assert error.value.code == 1008


@pytest.mark.parametrize("method,path", [("GET", "/"), ("GET", "/health"), ("GET", "/subscriptions"), ("GET", "/docs"), ("GET", "/openapi.json"), ("POST", "/forecast"), ("POST", "/subscriptions"), ("POST", "/update")])
def test_alternative_app_still_cannot_bypass_access_control(monkeypatch, method, path):
    from agrocast.serve import api

    config = Mock(side_effect=AssertionError("legacy engine must not run"))
    monkeypatch.setattr(api, "get_config", config)
    with TestClient(api.app) as client:
        response = client.request(method, path, json={})
    assert response.status_code == 410
    config.assert_not_called()


def test_canonical_application_has_guard_and_no_openapi_route(application):
    assert any(item.cls is AccessGuard for item in application.user_middleware)
    assert application.openapi_url is None
    assert not hasattr(product, "app")


def test_a_future_nested_route_cannot_hide_behind_resource_id_policy(application, clients):
    called = []

    @application.get("/api/fields/export-all")
    def unsafe_export():
        called.append(True)
        return {"private": True}

    assert clients("admin_a").get("/api/fields/export-all").status_code == 403
    assert not called
