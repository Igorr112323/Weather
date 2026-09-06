import secrets
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError

from agrocast.identity.credentials import IdentityError, Role
from agrocast.identity.schema import fields, users


def test_admin_manages_only_users_in_own_organization(clients):
    admin = clients("admin_a")
    own = admin.get("/api/admin/users").json()["users"]
    assert {user["username"] for user in own} == {"reader_a", "operator_a", "admin_a"}
    foreign = clients("admin_b").get("/api/auth/me").json()["user"]
    response = admin.patch("/api/admin/users/" + foreign["id"], json={"role": "reader"})
    assert response.status_code == 404
    response = admin.post("/api/admin/users", json={"username": "new_reader", "password": secrets.token_urlsafe(32), "role": "reader"})
    assert response.status_code == 201
    assert response.json()["user"]["organization_id"] == own[0]["organization_id"]
    assert "password" not in response.text


def test_duplicate_username_is_not_an_upsert(clients, account_password):
    admin = clients("admin_a")
    response = admin.post("/api/admin/users", json={"username": "operator_b", "password": secrets.token_urlsafe(32), "role": "admin"})
    assert response.status_code == 409
    assert account_password not in response.text
    assert clients("operator_b").get("/api/auth/me").json()["user"]["role"] == "operator"


@pytest.mark.parametrize("change", [{"role": "reader"}, {"active": False}])
def test_cannot_demote_or_disable_last_active_admin(clients, change):
    admin = clients("admin_a")
    own = admin.get("/api/auth/me").json()["user"]
    response = admin.patch("/api/admin/users/" + own["id"], json=change)
    assert response.status_code == 409
    assert response.json()["code"] == "last_admin_required"
    assert admin.get("/api/auth/me").status_code == 200


def test_role_changes_revoke_sessions_and_take_effect_after_login(clients, identity, account_password):
    target = clients("operator_a")
    own = target.get("/api/auth/me").json()["user"]
    field = target.post("/api/fields", json={"name": "Preserved", "point_id": "P01", "area_ha": 2}).json()["field"]
    cached = identity.login("operator_a", account_password).principal
    response = clients("admin_a").patch("/api/admin/users/" + own["id"], json={"role": "reader"})
    assert response.status_code == 200
    assert target.get("/api/fields").status_code == 401
    with pytest.raises(IdentityError) as error:
        identity.write_resource("fields", cached, {"name": "stale identity"})
    assert error.value.status == 401
    result = target.post("/api/auth/login", json={"username": "operator_a", "password": account_password})
    assert result.status_code == 200
    target.headers["X-CSRF-Token"] = result.json()["csrf_token"]
    assert target.get("/api/fields/" + field["id"]).status_code == 200
    assert target.post("/api/fields", json={"name": "Forbidden", "point_id": "P01", "area_ha": 1}).status_code == 403


def test_disabled_accounts_lose_existing_access_and_cannot_login(clients, account_password):
    target = clients("operator_a")
    own = target.get("/api/auth/me").json()["user"]
    admin = clients("admin_a")
    assert admin.patch("/api/admin/users/" + own["id"], json={"active": False}).status_code == 200
    assert target.get("/api/subscriptions").status_code == 401
    response = target.post("/api/auth/login", json={"username": "operator_a", "password": account_password})
    assert response.status_code == 401
    assert admin.patch("/api/admin/users/" + own["id"], json={"active": True}).status_code == 200
    assert target.get("/api/auth/me").status_code == 401


@pytest.mark.parametrize("extra", [{"organization_id": "other"}, {"id": "other"}, {"active": True}])
def test_user_creation_rejects_privilege_overposting(clients, extra):
    response = clients("admin_a").post("/api/admin/users", json={"username": "created", "password": secrets.token_urlsafe(32), "role": "reader", **extra})
    assert response.status_code == 422


def test_identity_audit_is_tenant_scoped_and_contains_no_credentials(clients, account_password):
    first = clients("admin_a")
    other = clients("admin_b")
    first.post("/api/admin/users", json={"username": "audited_user", "password": account_password, "role": "reader"})
    events = first.get("/api/admin/events").json()["events"]
    assert any(event["action"] == "user.created" for event in events)
    organization_id = first.get("/api/auth/me").json()["user"]["organization_id"]
    assert all(event["organization_id"] == organization_id for event in events)
    assert all(event["organization_id"] != organization_id for event in other.get("/api/admin/events").json()["events"])
    assert account_password not in str(events)
    assert first.headers["X-CSRF-Token"] not in str(events)


def test_database_enforces_owner_organization_membership(clients, identity_engine, clock):
    first = clients("operator_a").get("/api/auth/me").json()["user"]
    second = clients("operator_b").get("/api/auth/me").json()["user"]
    with pytest.raises(IntegrityError):
        with identity_engine.begin() as connection:
            connection.execute(insert(fields).values(id=str(uuid4()), owner_id=first["id"], organization_id=second["organization_id"], data={}, created_at=clock[0], updated_at=clock[0]))


def test_password_and_role_are_not_editable_through_user_patch(clients):
    admin = clients("admin_a")
    user_id = admin.get("/api/admin/users").json()["users"][1]["id"]
    for data in ({"role": "superadmin"}, {"active": "false"}, {"password": "new password"}, {"organization_id": str(uuid4())}, {}):
        assert admin.patch("/api/admin/users/" + user_id, json=data).status_code == 422


def test_admin_change_is_serialized_on_postgresql(identity, account_password, identity_engine):
    if identity_engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL row-lock integration test")
    first = identity.login("admin_a", account_password).principal
    identity.create_user(first, "second_admin", account_password, Role.ADMIN)
    second = identity.login("second_admin", account_password).principal
    barrier = Barrier(2)

    def demote(principal):
        barrier.wait(timeout=10)
        try:
            identity.update_user(principal, principal.id, Role.READER)
            return 200
        except IdentityError as error:
            return error.status

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(demote, (first, second)))
    assert sorted(outcomes) == [200, 409]
    with identity_engine.connect() as connection:
        rows = connection.execute(select(users).where(users.c.organization_id == first.organization_id, users.c.role == "admin", users.c.active.is_(True))).all()
    assert len(rows) == 1
