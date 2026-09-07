import json

from sqlalchemy import func, select

from agrocast.identity.schema import jobs, organizations, sessions, users


def create_field(client, name):
    response = client.post("/api/fields", json={"name": name, "point_id": "P01", "area_ha": 10})
    assert response.status_code == 201
    return response.json()["field"]


def test_export_contains_all_user_data_and_no_secrets(clients, identity_engine, account_password):
    client = clients("operator_a")
    me = client.get("/api/auth/me").json()["user"]
    field = create_field(client, "Экспортное поле")
    client.post("/api/subscriptions", json={"name": "Draft", "field_id": field["id"], "start_month": 10})
    raw = client.get("/api/account/export")
    assert raw.status_code == 200, raw.text
    export = raw.json()["export"]
    assert export["user"]["id"] == me["id"]
    assert export["user"]["username"] == "operator_a"
    assert "password_hash" not in json.dumps(export)
    assert account_password not in json.dumps(export)
    names = [row["data"]["name"] for row in export["resources"]["fields"]]
    assert "Экспортное поле" in names
    assert len(export["resources"]["subscriptions"]) == 1
    assert export["sessions_active"] == 1


def test_delete_account_requires_password_and_cascades(clients, identity_engine, account_password):
    client = clients("operator_a")
    me = client.get("/api/auth/me").json()["user"]
    create_field(client, "Удаляемое поле")
    wrong = client.request("DELETE", "/api/account", json={"password": "wrong-password-123"})
    assert wrong.status_code == 403
    with identity_engine.begin() as connection:
        connection.execute(jobs.insert().values(
            id="job-active-1", organization_id=me["organization_id"], owner_id=me["id"],
            data={"kind": "forecast"}, status="queued", dedup_sha256="a" * 64,
            created_at=1, updated_at=1,
        ))
    blocked = client.request("DELETE", "/api/account", json={"password": account_password})
    assert blocked.status_code == 409
    with identity_engine.begin() as connection:
        connection.execute(jobs.delete().where(jobs.c.id == "job-active-1"))
    ok = client.request("DELETE", "/api/account", json={"password": account_password})
    assert ok.status_code == 204
    with identity_engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(users).where(users.c.id == me["id"])).scalar_one() == 0
        assert connection.execute(select(func.count()).select_from(sessions).where(sessions.c.user_id == me["id"])).scalar_one() == 0
        assert connection.execute(select(func.count()).select_from(organizations).where(organizations.c.id == me["organization_id"])).scalar_one() == 1
        assert connection.execute(select(func.count()).select_from(users).where(users.c.username == "admin_a")).scalar_one() == 1
    after_delete = client.get("/api/auth/me")
    assert after_delete.status_code == 401


