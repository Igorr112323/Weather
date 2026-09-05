import os
import secrets
import time
from contextlib import ExitStack
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, insert
from sqlalchemy.schema import CreateSchema, DropSchema

from agrocast.identity.credentials import passwords
from agrocast.core.settings import RuntimeSettings
from agrocast.identity.database import migrate
from agrocast.identity.schema import organizations, users
from agrocast.identity.service import IdentityService
from agrocast.serve.product import create_app

ORIGIN = "https://testserver"


@pytest.fixture(scope="session")
def account_password():
    return secrets.token_urlsafe(32)


@pytest.fixture(scope="session")
def account_password_hash(account_password):
    return passwords.hash(account_password)


@pytest.fixture
def identity_engine(tmp_path):
    configuration = os.environ.get("AGROCAST_TEST_DATABASE_URL_FILE")
    if configuration:
        url = Path(configuration).read_text().strip()
        schema = "test_" + uuid4().hex
        admin = create_engine(url, hide_parameters=True)
        with admin.begin() as connection:
            connection.execute(CreateSchema(schema))
        engine = create_engine(url, hide_parameters=True, connect_args={"options": "-csearch_path=" + schema})
        try:
            migrate(engine)
            yield engine
        finally:
            engine.dispose()
            with admin.begin() as connection:
                connection.execute(DropSchema(schema, cascade=True))
            admin.dispose()
    else:
        engine = create_engine("sqlite:///" + str(tmp_path / "identity.db"), hide_parameters=True)

        @event.listens_for(engine, "connect")
        def configure(connection, record):
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")

        migrate(engine)
        try:
            yield engine
        finally:
            engine.dispose()


@pytest.fixture
def clock():
    return [int(time.time())]


@pytest.fixture
def identity(identity_engine, account_password_hash, clock):
    service = IdentityService(identity_engine, ORIGIN, clock=lambda: clock[0])
    with identity_engine.begin() as connection:
        for organization in ("a", "b"):
            organization_id = str(uuid4())
            connection.execute(insert(organizations).values(id=organization_id, name=organization, active=True, created_at=clock[0]))
            for role in ("reader", "operator", "admin"):
                connection.execute(insert(users).values(
                    id=str(uuid4()), organization_id=organization_id, username=role + "_" + organization,
                    role=role, active=True, password_hash=account_password_hash, created_at=clock[0],
                ))
    return service


@pytest.fixture
def runtime_settings(tmp_path):
    return RuntimeSettings(state_dir=tmp_path / "state", public_origin=ORIGIN)


@pytest.fixture
def application(identity, runtime_settings):
    return create_app(identity, settings=runtime_settings)


@pytest.fixture
def anonymous(application):
    with TestClient(application, base_url=ORIGIN) as client:
        yield client


@pytest.fixture
def clients(application, account_password):
    cached = {}
    with ExitStack() as stack:
        def logged_in(username="operator_a"):
            if username not in cached:
                client = stack.enter_context(TestClient(application, base_url=ORIGIN, headers={"Origin": ORIGIN}))
                response = client.post("/api/auth/login", json={"username": username, "password": account_password})
                assert response.status_code == 200
                client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
                cached[username] = client
            return cached[username]

        yield logged_in
