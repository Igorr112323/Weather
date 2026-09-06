import json
from unittest.mock import Mock
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from agrocast.core.contracts import ForecastSpec, RegionFieldSpec
from agrocast.identity.schema import crops
from agrocast.serve import pipeline, product, region
from agrocast.serve.errors import ErrorResponse
from agrocast.serve.responses import accepted_job_response

VALID_POINT = {"lat": 46.25, "lon": 38.25, "point_id": "P01", "start": "2026-10", "horizon": 3, "mode": "seasonal", "season_len": 3}


@pytest.fixture
def no_worker(monkeypatch):
    worker = Mock(side_effect=AssertionError("request validation must precede computation"))
    monkeypatch.setattr(pipeline, "start_job", worker)
    monkeypatch.setattr(region, "build_field", worker)
    return worker


@pytest.mark.parametrize("updates", [
    {"lat": 91}, {"lat": -91}, {"lon": 181}, {"lon": -181}, {"lat": True}, {"lon": "38.25"},
    {"start": "2026-00"}, {"start": "2026-13"}, {"start": "2026-1"}, {"start": "2026-10-01"}, {"start": " 2026-10"}, {"start": ""},
    {"horizon": 0}, {"horizon": 7}, {"horizon": 1.5}, {"horizon": "3"}, {"horizon": True}, {"horizon": 4},
    {"mode": "MONTHLY"}, {"mode": "unknown"}, {"mode": "monthly", "season_len": 3},
    {"season_len": 0}, {"season_len": 4}, {"season_len": "3"}, {"kind": "other"},
    {"variables": []}, {"variables": ["humidity"]}, {"variables": ["tp", "tp"]},
    {"year": 2020}, {"owner_id": str(uuid4())}, {"data_release": "fake"}, {"variety": "legacy name"},
    {"variety_id": str(uuid4())}, {"variety_revision": 2}, {"variety_revision": 0, "variety_id": str(uuid4())},
    {"kind": "hindcast", "start": "2003-12"}, {"kind": "hindcast", "start": "2024-11"}, {"start": "2099-12"},
])
def test_invalid_point_contract_rejected_before_worker(clients, no_worker, updates):
    response = clients().post("/api/prepare", json={**VALID_POINT, **updates})
    assert response.status_code == 422
    assert ErrorResponse.model_validate(response.json()).code == "invalid_request"
    no_worker.assert_not_called()


@pytest.mark.parametrize("updates", [
    {"start": "2026-13"}, {"start": ""}, {"start": "2099-12"}, {"mode": "monthly"},
    {"horizon": 6}, {"horizon": "3"}, {"season_len": 1}, {"season_len": 3.0}, {"region": "../krai"},
    {"variables": ["tp"]}, {"owner_id": str(uuid4())},
])
def test_invalid_region_contract_rejected_before_worker(clients, no_worker, updates):
    response = clients().post("/api/region/refresh", json={"start": "2026-10", **updates})
    assert response.status_code == 422
    no_worker.assert_not_called()


@pytest.mark.parametrize("path,payload", [("/api/prepare", VALID_POINT), ("/api/region/refresh", {"start": "2026-03"})])
@pytest.mark.parametrize("username", ["reader_a", "operator_a", "admin_a"])
def test_valid_shapes_do_not_enable_computation(clients, no_worker, path, payload, username):
    before = dict(product.JOBS)
    response = clients(username).post(path, json=payload)
    assert response.status_code == 403
    code = "role_forbidden" if username.startswith("reader") else "pilot_operation_disabled"
    assert response.json()["code"] == code
    assert product.JOBS == before
    no_worker.assert_not_called()


@pytest.mark.parametrize("changes,code", [({"region": "rostov"}, "pilot_region_disabled"), ({"lat": 45.03, "lon": 39.07}, "pilot_point_disabled"), ({"point_id": "P02"}, "invalid_request")])
def test_support_matrix_rejects_unapproved_targets(clients, no_worker, changes, code):
    response = clients().post("/api/prepare", json={**VALID_POINT, **changes})
    assert response.status_code == (422 if code == "invalid_request" else 403)
    assert response.json()["code"] == code
    no_worker.assert_not_called()


@pytest.mark.parametrize("body", ['{"lat":NaN}', '{"lat":Infinity}', '{"lat":1e999}', '{"start":"2026-03","start":"2026-10"}', '[1,2]', '{not json}'])
def test_ambiguous_or_invalid_json_has_uniform_errors(clients, no_worker, body):
    response = clients().post("/api/prepare", content=body, headers={"Content-Type": "application/json"})
    assert response.status_code == 422
    assert ErrorResponse.model_validate(response.json()).code == "invalid_request"
    no_worker.assert_not_called()


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "limit=1.0", "limit=true", "limit=2&limit=3", "owner_id=other", "limit=1&extra=2"])
def test_query_contract_does_not_ignore_unsupported_parameters(clients, query):
    response = clients().get("/api/fields?" + query)
    assert response.status_code == 422
    assert ErrorResponse.model_validate(response.json()).code == "invalid_request"


def test_monthly_default_is_explicit_and_invalid_explicit_length_is_not_corrected():
    payload = {key: value for key, value in VALID_POINT.items() if key != "season_len"}
    payload["mode"] = "monthly"
    assert ForecastSpec.model_validate(payload).season_len == 1
    with pytest.raises(ValidationError):
        ForecastSpec.model_validate({**payload, "season_len": 3})
    with pytest.raises(ValidationError):
        RegionFieldSpec(start="2026-03", horizon=True)


def test_crop_revisions_change_within_one_second_and_name_is_not_identity(clients, identity_engine):
    client = clients("admin_a")
    body = {"name": 'Кукуруза / "<сорт>"', "breeder": "Селекционер & Юникод", "gdd": 2000}
    first = client.post("/api/crops", json=body).json()["crop"]
    assert first["revision"] == 1
    second = client.put("/api/crops/" + first["id"], json={**body, "name": "Другое имя", "gdd": 2100}).json()["crop"]
    assert second["id"] == first["id"]
    assert second["revision"] == 2
    assert second["updated_at"] == first["updated_at"]
    assert client.get("/api/crops/" + first["id"]).json()["crop"] == second
    with identity_engine.connect() as connection:
        assert len(connection.execute(select(crops)).all()) == 1


def test_subscription_preserves_complete_parameters_and_scopes_variety(clients):
    client = clients("admin_a")
    field = client.post("/api/fields", json={"name": "Поле", "point_id": "P01", "area_ha": 20}).json()["field"]
    crop = client.post("/api/crops", json={"name": "Сорт"}).json()["crop"]
    body = {"name": "Март", "field_id": field["id"], "start_month": 3, "horizon": 2, "mode": "monthly", "season_len": 1, "variables": ["tp"], "variety_id": crop["id"], "variety_revision": 1, "active": False}
    response = client.post("/api/subscriptions", json=body)
    assert response.status_code == 201
    subscription = response.json()["subscription"]
    assert subscription["data"] == body
    assert client.get("/api/subscriptions/" + subscription["id"]).json()["subscription"]["data"] == body
    changed = {**body, "start_month": 10, "name": "Октябрь"}
    assert client.put("/api/subscriptions/" + subscription["id"], json=changed).json()["subscription"]["data"] == changed
    client.put("/api/crops/" + crop["id"], json={"name": "Новая версия"})
    assert client.post("/api/subscriptions", json=body).status_code == 409
    foreign = clients("admin_b").post("/api/crops", json={"name": "Чужой сорт"}).json()["crop"]
    assert client.post("/api/subscriptions", json={**body, "variety_id": foreign["id"]}).status_code == 404


@pytest.mark.parametrize("value", [0, 1, "false", True])
def test_subscription_activation_requires_an_actual_false_boolean(clients, value):
    response = clients().post("/api/subscriptions", json={"name": "Draft", "field_id": str(uuid4()), "start_month": 3, "active": value})
    assert response.status_code == 422


def test_response_validation_failure_is_safe_and_typed(clients, identity, monkeypatch):
    client = clients()
    secret = "do-not-return-internal-record"
    monkeypatch.setattr(identity, "list_resources", lambda *args: [{"private": secret}])
    response = client.get("/api/fields")
    assert response.status_code == 500
    assert response.json() == {"code": "invalid_response", "error": "invalid_response"}
    assert secret not in response.text


def test_artifact_errors_are_not_success_responses(clients, application, tmp_path, monkeypatch):
    monkeypatch.setattr(application.state, "settings", application.state.settings.with_paths(world_dir=tmp_path / "missing-world"))
    tmp_path = tmp_path / "missing-world"
    tmp_path.mkdir()
    client = clients()
    for body in (None, '{"bad": NaN}', '{"bad":1e999}', '{"key":1,"key":2}'):
        artifact = tmp_path / "artifacts/value_report.json"
        artifact.parent.mkdir(exist_ok=True)
        if body is not None:
            artifact.write_text(body)
        response = client.get("/api/value")
        assert response.status_code == 503
        assert ErrorResponse.model_validate(response.json()).code == "artifact_unavailable"


def test_openapi_has_typed_responses_auth_and_disabled_async_contract(clients, anonymous):
    assert anonymous.get("/api/contracts").status_code == 401
    schema = clients().get("/api/contracts").json()
    assert schema["x-pilot-computation-enabled"] is False
    paths = schema["paths"]
    for path in ("/api/prepare", "/api/region/refresh"):
        operation = paths[path]["post"]
        assert "202" in operation["responses"] and "200" not in operation["responses"]
        assert operation["responses"]["202"]["content"]["application/json"]["schema"]["$ref"].endswith("/AcceptedJob")
        assert operation["x-pilot-admission"] == "disabled"
        assert operation["security"] == [{"SessionCookie": [], "CSRF": []}]
    assert paths["/api/fields"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith("/FieldsResponse")
    assert paths["/api/auth/login"]["post"].get("security", []) == []
    assert "ErrorResponse" in schema["components"]["schemas"]
    job_id = uuid4()
    response = accepted_job_response(job_id)
    assert response.status_code == 202 and response.headers["location"] == "/api/jobs/" + str(job_id)
    assert json.loads(response.body)["status"] == "queued"
