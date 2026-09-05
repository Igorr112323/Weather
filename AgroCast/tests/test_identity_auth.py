import secrets
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError

from agrocast.identity.credentials import IdentityError, Role, passwords
from agrocast.identity.database import IdentitySettings
from agrocast.identity.schema import login_limits, organizations, sessions, users
from agrocast.identity.service import IdentityService, token_hash
from agrocast.serve.product import create_app
from agrocast.serve.security import SESSION_COOKIE

ORIGIN = "https://testserver"


@pytest.mark.parametrize("username", ["reader_a", "operator_a", "admin_a"])
def test_individual_login_and_cookie_security(anonymous, account_password, identity_engine, username):
    response = anonymous.post("/api/auth/login", headers={"Origin": ORIGIN}, json={"username": username, "password": account_password})
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(SESSION_COOKIE + "=")
    for attribute in ("HttpOnly", "Secure", "SameSite=lax", "Path=/", "Max-Age=28800"):
        assert attribute in cookie
    assert "Domain=" not in cookie
    token = anonymous.cookies.get(SESSION_COOKIE)
    assert token not in response.text
    assert account_password not in response.text
    assert response.json()["user"]["role"] == username.split("_")[0]
    assert anonymous.get("/api/auth/me").json()["user"]["username"] == username
    with identity_engine.connect() as connection:
        row = connection.execute(select(sessions)).mappings().one()
        assert row["token_hash"] == token_hash(token)
        assert token not in str(dict(row))
        user = connection.execute(select(users).where(users.c.username == username)).mappings().one()
        assert user["password_hash"].startswith("$argon2id$")
        assert account_password not in user["password_hash"]


@pytest.mark.parametrize("username", ["operator_a", "unknown_user", "admin_b"])
def test_invalid_login_is_generic(anonymous, username):
    response = anonymous.post("/api/auth/login", headers={"Origin": ORIGIN}, json={"username": username, "password": "not the correct password"})
    assert response.status_code == 401
    assert response.json() == {"code": "invalid_credentials", "error": "invalid_credentials"}
    assert "set-cookie" not in response.headers


def test_disabled_user_cannot_login(anonymous, identity_engine, account_password):
    with identity_engine.begin() as connection:
        connection.execute(update(users).where(users.c.username == "operator_a").values(active=False))
    response = anonymous.post("/api/auth/login", headers={"Origin": ORIGIN}, json={"username": "operator_a", "password": account_password})
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_credentials"


@pytest.mark.parametrize("origin", [None, "null", "http://testserver", "https://evil.example", "https://testserver.evil.example"])
def test_login_rejects_missing_or_foreign_origin(anonymous, account_password, identity_engine, origin):
    headers = {} if origin is None else {"Origin": origin}
    response = anonymous.post("/api/auth/login", headers=headers, json={"username": "operator_a", "password": account_password})
    assert response.status_code == 403
    with identity_engine.connect() as connection:
        assert not connection.execute(select(sessions)).all()


def test_login_rejects_forms_and_duplicate_origin(anonymous, account_password):
    response = anonymous.post("/api/auth/login", headers={"Origin": ORIGIN}, data={"username": "operator_a", "password": account_password})
    assert response.status_code == 415
    response = anonymous.post("/api/auth/login", headers=[("Origin", ORIGIN), ("Origin", ORIGIN)], json={})
    assert response.status_code == 403


def test_credentials_are_not_reflected_by_validation_errors(anonymous, account_password):
    response = anonymous.post("/api/auth/login", headers={"Origin": ORIGIN}, json={"username": "bad username", "password": account_password, "role": "admin"})
    assert response.status_code == 422
    assert account_password not in response.text
    assert all("input" not in detail and "ctx" not in detail for detail in response.json()["detail"])


def test_password_whitespace_is_not_silently_changed(identity, anonymous):
    password = "  a deliberate spaced password  "
    identity.bootstrap("whitespace", "spaces", password)
    response = anonymous.post("/api/auth/login", headers={"Origin": ORIGIN}, json={"username": "spaces", "password": password.strip()})
    assert response.status_code == 401
    response = anonymous.post("/api/auth/login", headers={"Origin": ORIGIN}, json={"username": "spaces", "password": password})
    assert response.status_code == 200


@pytest.mark.parametrize("headers", [
    {"Origin": "https://evil.example"},
    {"Origin": ORIGIN, "X-CSRF-Token": "incorrect"},
])
def test_mutations_require_correct_origin_and_session_csrf(clients, headers):
    client = clients()
    response = client.post("/api/fields", headers=headers, json={"name": "Field", "point_id": "P01", "area_ha": 10})
    assert response.status_code == 403
    assert client.get("/api/fields").json()["fields"] == []


def test_mutations_reject_absent_or_other_session_csrf(clients):
    first = clients("operator_a")
    second = clients("operator_b")
    csrf = first.headers.pop("X-CSRF-Token")
    assert first.post("/api/auth/logout").status_code == 403
    assert first.post("/api/auth/logout", headers={"X-CSRF-Token": second.headers["X-CSRF-Token"]}).status_code == 403
    assert first.post("/api/auth/logout", headers=[("X-CSRF-Token", csrf), ("X-CSRF-Token", csrf)]).status_code == 403
    assert first.get("/api/auth/me").status_code == 200


def test_logout_revokes_server_session_and_cookie(clients, anonymous, identity):
    client = clients()
    token = client.cookies.get(SESSION_COOKIE)
    response = client.post("/api/auth/logout")
    assert response.status_code == 204
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert client.get("/api/auth/me").status_code == 401
    with pytest.raises(IdentityError) as error:
        identity.authenticate(token)
    assert error.value.status == 401
    assert anonymous.get("/api/auth/me", headers={"Cookie": f"{SESSION_COOKIE}={token}"}).status_code == 401


def test_sessions_expire_at_absolute_deadline(clients, clock, identity):
    client = clients()
    clock[0] += identity.session_seconds
    assert client.get("/api/auth/me").status_code == 401


def test_disabling_organization_revokes_access(clients, identity_engine):
    client = clients()
    organization_id = client.get("/api/auth/me").json()["user"]["organization_id"]
    with identity_engine.begin() as connection:
        connection.execute(update(organizations).where(organizations.c.id == organization_id).values(active=False))
    assert client.get("/api/fields").status_code == 401


def test_session_survives_another_app_and_logout_is_shared(clients, identity, account_password):
    first = clients()
    restarted = IdentityService(identity.engine, ORIGIN, clock=identity.clock)
    with TestClient(create_app(restarted), base_url=ORIGIN, headers={"Origin": ORIGIN}) as second:
        second.cookies.update(first.cookies)
        response = second.get("/api/auth/me")
        assert response.status_code == 200
        assert response.json()["user"] == first.get("/api/auth/me").json()["user"]
        second.headers["X-CSRF-Token"] = response.json()["csrf_token"]
        assert second.post("/api/auth/logout").status_code == 204
    assert first.get("/api/auth/me").status_code == 401


@pytest.mark.parametrize("token", ["", "short", "x" * 44, "x" * 1000, "пароль", "../invalid"])
def test_invalid_session_tokens_never_authenticate(anonymous, token):
    if token.isascii():
        response = anonymous.get("/api/auth/me", headers={"Cookie": f"{SESSION_COOKIE}={token}"})
        assert response.status_code == 401
    else:
        with pytest.raises(IdentityError):
            anonymous.app.state.identity.authenticate(token)


def test_duplicate_session_cookie_is_rejected(clients):
    client = clients()
    token = client.cookies.get(SESSION_COOKIE)
    response = client.get("/api/auth/me", headers={"Cookie": f"{SESSION_COOKIE}={token}; {SESSION_COOKIE}={token}"})
    assert response.status_code == 401


def test_basic_bearer_forwarded_identity_and_role_headers_do_not_grant_access(anonymous, account_password):
    assert anonymous.get("/api/fields", auth=("admin_a", account_password)).status_code == 401
    response = anonymous.get("/api/fields", headers={
        "Authorization": "Bearer " + secrets.token_urlsafe(32),
        "X-Forwarded-For": "127.0.0.1", "X-Forwarded-User": "admin_a",
        "X-Auth-Request-User": "admin_a", "X-AgroCast-Role": "admin",
    })
    assert response.status_code == 401


def test_login_limit_is_persistent_across_app_instances(identity, anonymous, clock):
    for _ in range(5):
        response = anonymous.post("/api/auth/login", headers={"Origin": ORIGIN}, json={"username": "unknown", "password": "wrong"})
        assert response.status_code == 401
    with TestClient(create_app(IdentityService(identity.engine, ORIGIN, clock=identity.clock)), base_url=ORIGIN) as second:
        response = second.post("/api/auth/login", headers={"Origin": ORIGIN}, json={"username": "unknown", "password": "wrong"})
        assert response.status_code == 429
        assert 1 <= int(response.headers["Retry-After"]) <= 60
        clock[0] += 60
        assert second.post("/api/auth/login", headers={"Origin": ORIGIN}, json={"username": "unknown", "password": "wrong"}).status_code == 401


def test_global_login_limit_cannot_be_bypassed_with_new_usernames(identity_engine, identity, anonymous):
    with identity_engine.begin() as connection:
        connection.execute(update(login_limits).where(login_limits.c.key == "global").values(window=identity._now() // 60, attempts=30))
    response = anonymous.post("/api/auth/login", headers={"Origin": ORIGIN}, json={"username": "new_unknown", "password": "wrong"})
    assert response.status_code == 429


def test_password_change_checks_current_secret_and_revokes_all_sessions(clients, identity, account_password):
    client = clients()
    second_session = identity.login("operator_a", account_password)
    replacement = secrets.token_urlsafe(32)
    response = client.post("/api/auth/password", json={"current_password": "incorrect", "new_password": replacement})
    assert response.status_code == 401
    response = client.post("/api/auth/password", json={"current_password": account_password, "new_password": replacement})
    assert response.status_code == 204
    assert client.get("/api/auth/me").status_code == 401
    with pytest.raises(IdentityError):
        identity.authenticate(second_session.token)
    with pytest.raises(IdentityError):
        identity.login("operator_a", account_password)
    assert identity.login("operator_a", replacement).principal.role == Role.OPERATOR


def test_missing_identity_configuration_does_not_fall_back_to_old_shared_secret(monkeypatch, tmp_path):
    secret = tmp_path / "old_pilot_secret"
    secret.write_text(secrets.token_urlsafe(32))
    monkeypatch.delenv("AGROCAST_DATABASE_URL_FILE", raising=False)
    monkeypatch.setenv("AGROCAST_PILOT_SECRET_FILE", str(secret))
    from agrocast.core.settings import ConfigurationError

    with pytest.raises(ConfigurationError, match="AGROCAST_DATABASE_URL_FILE is required"):
        with TestClient(create_app(), base_url=ORIGIN):
            pytest.fail("startup must fail without personal identity configuration")


@pytest.mark.parametrize("origin", ["", "http://testserver", "https://*.example.com", "https://user:secret@example.com", "https://example.com/path", "https://example.com?query=1"])
def test_public_origin_must_be_explicit_https(origin):
    with pytest.raises(ValueError):
        IdentitySettings("not returned by repr", origin)


def test_settings_repr_hides_database_credential():
    settings = IdentitySettings("secret_database_connection", ORIGIN)
    assert "secret_database_connection" not in repr(settings)


def test_database_errors_are_fail_closed_without_credentials_in_body(anonymous, identity, monkeypatch):
    secret = secrets.token_urlsafe(32)
    broken = Mock(side_effect=OperationalError("hidden", {"password": secret}, Exception(secret)))
    monkeypatch.setattr(identity, "authenticate", broken)
    response = anonymous.get("/api/fields")
    assert response.status_code == 503
    assert secret not in response.text


def test_password_hash_work_has_a_bounded_capacity(account_password_hash, account_password):
    assert passwords.capacity.acquire(blocking=False)
    assert passwords.capacity.acquire(blocking=False)
    try:
        with pytest.raises(IdentityError) as error:
            passwords.verify(account_password_hash, account_password)
        assert error.value.status == 503
    finally:
        passwords.capacity.release()
        passwords.capacity.release()


def test_session_and_password_are_not_logged(clients, account_password, caplog):
    client = clients()
    token = client.cookies.get(SESSION_COOKIE)
    principal = client.get("/api/auth/me").json()
    assert account_password not in caplog.text
    assert token not in caplog.text
    assert principal["csrf_token"] not in caplog.text


def test_html_login_uses_no_local_storage_or_inline_secrets(anonymous):
    response = anonymous.get("/login")
    assert response.status_code == 200
    assert 'type="password"' in response.text
    assert 'autocomplete="current-password"' in response.text
    script = anonymous.get("/login.js").text
    assert "localStorage" not in script
    assert "sessionStorage" not in script
    assert "innerHTML" not in script


def test_oversized_and_chunked_login_bodies_are_rejected_before_hashing(anonymous, identity, monkeypatch):
    login = Mock(side_effect=AssertionError("oversized input must not reach login"))
    monkeypatch.setattr(identity, "login", login)
    for body in (b"x" * 16385, (b"x" * 8193 for _ in range(3))):
        response = anonymous.post("/api/auth/login", headers={"Origin": ORIGIN, "Content-Type": "application/json"}, content=body)
        assert response.status_code == 413
    login.assert_not_called()


def test_cross_site_forgery_cannot_use_forged_host_to_change_allowed_origin(clients):
    client = clients()
    response = client.post("/api/auth/logout", headers={"Origin": "https://evil.example", "Host": "evil.example", "X-Forwarded-Host": "testserver"})
    assert response.status_code == 403
    assert client.get("/api/auth/me").status_code == 200


def test_non_ascii_csrf_is_rejected_without_server_error(clients):
    response = clients().post("/api/auth/logout", headers={b"X-CSRF-Token": b"\xff" * 43})
    assert response.status_code == 403
