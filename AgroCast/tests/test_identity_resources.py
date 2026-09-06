from uuid import uuid4

import pytest
from sqlalchemy import insert
from sqlalchemy.exc import IntegrityError

from agrocast.identity.credentials import IdentityError
from agrocast.identity.schema import jobs, subscriptions


def create_field(client, name="Field"):
    response = client.post("/api/fields", json={"name": name, "point_id": "P01", "area_ha": 100})
    assert response.status_code == 201
    return response.json()["field"]


def create_subscription(client, field_id):
    response = client.post("/api/subscriptions", json={"name": "Draft", "field_id": field_id, "start_month": 10})
    assert response.status_code == 201
    return response.json()["subscription"]


@pytest.mark.parametrize("username", ["operator_a", "admin_a"])
def test_owner_fields_and_draft_subscriptions_round_trip(clients, username):
    client = clients(username)
    me = client.get("/api/auth/me").json()["user"]
    field = create_field(client, 'Северное "поле" </script>')
    assert field["owner_id"] == me["id"]
    assert field["organization_id"] == me["organization_id"]
    assert client.get("/api/fields/" + field["id"]).json()["field"] == field
    response = client.put("/api/fields/" + field["id"], json={"name": "Renamed", "point_id": "P02", "area_ha": 120})
    assert response.status_code == 200
    assert response.json()["field"]["data"]["name"] == "Renamed"
    subscription = create_subscription(client, field["id"])
    assert subscription["owner_id"] == me["id"]
    assert subscription["data"]["start_month"] == 10
    assert subscription["data"]["active"] is False
    assert client.get("/api/subscriptions").json()["subscriptions"] == [subscription]
    response = client.put("/api/subscriptions/" + subscription["id"], json={"name": "March", "field_id": field["id"], "start_month": 3, "horizon": 1, "mode": "monthly"})
    assert response.status_code == 200
    assert response.json()["subscription"]["data"]["start_month"] == 3
    assert client.delete("/api/subscriptions/" + subscription["id"]).status_code == 204
    assert client.delete("/api/fields/" + field["id"]).status_code == 204


@pytest.mark.parametrize("attacker", ["reader_a", "admin_a", "operator_b", "admin_b"])
@pytest.mark.parametrize("kind", ["fields", "subscriptions", "jobs"])
def test_users_cannot_read_other_owners_even_as_same_org_admin(clients, identity_engine, clock, attacker, kind):
    owner = clients("operator_a")
    field = create_field(owner)
    if kind == "fields":
        resource = field
    elif kind == "subscriptions":
        resource = create_subscription(owner, field["id"])
    else:
        resource = {"id": str(uuid4()), "owner_id": field["owner_id"], "organization_id": field["organization_id"]}
        with identity_engine.begin() as connection:
            connection.execute(insert(jobs).values(**resource, status="succeeded", data={"report": "private"}, created_at=clock[0], updated_at=clock[0]))
    client = clients(attacker)
    assert client.get(f'/api/{kind}/{resource["id"]}').status_code == 404
    assert client.get(f'/api/{kind}', params={"owner_id": field["owner_id"], "organization_id": field["organization_id"]}).status_code == 422
    assert client.get(f'/api/{kind}').json()[kind] == []
    if kind == "jobs":
        assert client.get('/api/job/' + resource["id"]).status_code == 404
        assert owner.get('/api/job/' + resource["id"]).json()["validation"]["status"] == "unverified"
    else:
        assert owner.get(f'/api/{kind}/{resource["id"]}').status_code == 200


@pytest.mark.parametrize("attacker", ["admin_a", "operator_b", "admin_b"])
@pytest.mark.parametrize("kind", ["fields", "subscriptions", "jobs"])
def test_other_owners_cannot_mutate_or_delete_resources(clients, identity_engine, clock, attacker, kind):
    owner = clients("operator_a")
    field = create_field(owner)
    if kind == "fields":
        resource = field
        data = {"name": "Hijack", "point_id": "P01", "area_ha": 1}
    elif kind == "subscriptions":
        resource = create_subscription(owner, field["id"])
        data = {"name": "Hijack", "field_id": field["id"], "start_month": 2}
    else:
        resource = {"id": str(uuid4()), "owner_id": field["owner_id"], "organization_id": field["organization_id"]}
        with identity_engine.begin() as connection:
            connection.execute(insert(jobs).values(**resource, status="succeeded", data={"private": True}, created_at=clock[0], updated_at=clock[0]))
    client = clients(attacker)
    if kind != "jobs":
        assert client.put(f'/api/{kind}/{resource["id"]}', json=data).status_code == 404
    assert client.delete(f'/api/{kind}/{resource["id"]}').status_code == 404
    assert owner.get(f'/api/{kind}/{resource["id"]}').status_code == 200


@pytest.mark.parametrize("username", ["reader_a", "operator_a"])
@pytest.mark.parametrize("method,path", [("POST", "/api/crops"), ("PUT", "/api/crops/{id}"), ("DELETE", "/api/crops/{id}"), ("POST", "/api/admin/users"), ("PATCH", "/api/admin/users/{id}"), ("GET", "/api/admin/users"), ("GET", "/api/admin/events"), ("GET", "/api/health")])
def test_insufficient_roles_are_denied_before_body_or_lookup(clients, username, method, path):
    client = clients(username)
    response = client.request(method, path.replace("{id}", str(uuid4())), content=b"invalid json")
    assert response.status_code == 403
    assert response.json()["code"] == "role_forbidden"


@pytest.mark.parametrize("method,path", [("POST", "/api/fields"), ("PUT", "/api/fields/{id}"), ("DELETE", "/api/fields/{id}"), ("POST", "/api/subscriptions"), ("DELETE", "/api/jobs/{id}")])
def test_reader_cannot_write_personal_resources(clients, method, path):
    response = clients("reader_a").request(method, path.replace("{id}", str(uuid4())), json={})
    assert response.status_code == 403


def test_subscription_cannot_reference_a_field_owned_by_someone_else(clients):
    field = create_field(clients("operator_a"))
    for username in ("admin_a", "operator_b"):
        response = clients(username).post("/api/subscriptions", json={"name": "Not mine", "field_id": field["id"], "start_month": 10})
        assert response.status_code == 404


def test_database_rejects_cross_owner_subscription_foreign_key(clients, identity_engine, clock):
    field = create_field(clients("operator_a"))
    other = clients("admin_a").get("/api/auth/me").json()["user"]
    with pytest.raises(IntegrityError):
        with identity_engine.begin() as connection:
            connection.execute(insert(subscriptions).values(
                id=str(uuid4()), owner_id=other["id"], organization_id=other["organization_id"],
                field_id=field["id"], data={}, created_at=clock[0], updated_at=clock[0],
            ))


def test_field_deletion_cascades_only_its_own_draft_subscriptions(clients):
    client = clients()
    field = create_field(client)
    draft = create_subscription(client, field["id"])
    second_field = create_field(client, "Other field")
    kept = create_subscription(client, second_field["id"])
    assert client.delete("/api/fields/" + field["id"]).status_code == 204
    assert client.get("/api/subscriptions/" + draft["id"]).status_code == 404
    assert client.get("/api/subscriptions/" + kept["id"]).status_code == 200


@pytest.mark.parametrize("key", ["owner_id", "organization_id", "id", "role", "status"])
def test_overposting_cannot_assign_owner_or_privileges(clients, key):
    response = clients().post("/api/fields", json={"name": "Field", "point_id": "P01", "area_ha": 2, key: str(uuid4())})
    assert response.status_code == 422
    assert clients().get("/api/fields").json()["fields"] == []


def test_draft_subscription_cannot_enable_execution(clients):
    client = clients()
    field = create_field(client)
    response = client.post("/api/subscriptions", json={"name": "Execute", "field_id": field["id"], "start_month": 10, "active": True})
    assert response.status_code == 422
    assert client.get("/api/subscriptions").json()["subscriptions"] == []


def test_crops_are_shared_only_within_organization_and_use_stable_ids(clients):
    admin = clients("admin_a")
    response = admin.post("/api/crops", json={"name": 'Гибрид / "<seed>"', "fao": 250})
    assert response.status_code == 201
    crop = response.json()["crop"]
    assert clients("reader_a").get("/api/crops").json()["crops"] == [crop]
    assert clients("admin_b").get("/api/crops").json()["crops"] == []
    assert clients("admin_b").get("/api/crops/" + crop["id"]).status_code == 404
    assert clients("admin_b").delete("/api/crops/" + crop["id"]).status_code == 404
    response = admin.put("/api/crops/" + crop["id"], json={"name": "Renamed / hybrid"})
    assert response.status_code == 200
    assert response.json()["crop"]["id"] == crop["id"]
    assert admin.delete("/api/crops/" + crop["id"]).status_code == 204


def test_active_job_cannot_be_deleted_before_durable_cancellation_exists(clients, identity_engine, clock):
    client = clients()
    user = client.get("/api/auth/me").json()["user"]
    job_id = str(uuid4())
    with identity_engine.begin() as connection:
        connection.execute(insert(jobs).values(id=job_id, owner_id=user["id"], organization_id=user["organization_id"], data={}, status="running", created_at=clock[0], updated_at=clock[0]))
    assert client.delete("/api/jobs/" + job_id).status_code == 409
    assert client.get("/api/jobs/" + job_id).status_code == 200


@pytest.mark.parametrize("body", [{"name": "", "point_id": "P01", "area_ha": 1}, {"name": "Field", "point_id": "P99", "area_ha": 1}, {"name": "Field", "point_id": "P01", "area_ha": -1}])
def test_new_resource_contracts_reject_invalid_scope_and_bounds(clients, body):
    assert clients().post("/api/fields", json=body).status_code == 422


def test_repository_rechecks_owner_and_role_without_http_middleware(identity, account_password, clients):
    owner = identity.login("operator_a", account_password).principal
    admin = identity.login("admin_a", account_password).principal
    resource = identity.write_resource("fields", owner, {"name": "Private", "point_id": "P01", "area_ha": 1})
    with pytest.raises(IdentityError) as error:
        identity.delete_resource("fields", admin, resource["id"])
    assert error.value.status == 404
    reader = identity.login("reader_a", account_password).principal
    with pytest.raises(IdentityError) as error:
        identity.write_resource("fields", reader, {})
    assert error.value.status == 403
