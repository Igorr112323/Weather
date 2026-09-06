from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, select

from agrocast.identity import cli
from agrocast.identity.credentials import IdentityError
from agrocast.identity.database import IdentitySettings, check_schema, migrate
from agrocast.identity.schema import metadata, sessions
from agrocast.identity.service import IdentityService
from agrocast.core.settings import ConfigurationError
from agrocast.serve.product import create_app
from fastapi.testclient import TestClient


@pytest.mark.parametrize("repeat", [1, 2])
def test_migration_is_idempotent_and_matches_metadata(identity_engine, repeat):
    for _ in range(repeat):
        migrate(identity_engine)
    check_schema(identity_engine)
    with identity_engine.connect() as connection:
        context = MigrationContext.configure(connection)
        assert compare_metadata(context, metadata) == []
    assert set(metadata.tables) <= set(inspect(identity_engine).get_table_names())


def test_initial_migration_can_be_reversed_and_reapplied(identity_engine):
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "migrations"))
    with identity_engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "base")
    assert set(metadata.tables).isdisjoint(inspect(identity_engine).get_table_names())
    migrate(identity_engine)
    check_schema(identity_engine)


def test_session_limit_evicts_old_sessions_and_keeps_newest(identity, account_password, clock, identity_engine):
    issued = []
    for _ in range(6):
        clock[0] += 60
        issued.append(identity.login("operator_a", account_password))
    with identity_engine.connect() as connection:
        assert len(connection.execute(select(sessions)).all()) == 5
    with pytest.raises(IdentityError):
        identity.authenticate(issued[0].token)
    assert identity.authenticate(issued[-1].token).id == issued[-1].principal.id
    assert issued[-1].token not in repr(issued[-1])
    assert issued[-1].principal.csrf_token not in repr(issued[-1])


def test_database_configuration_is_loaded_from_file_only(tmp_path, monkeypatch):
    path = tmp_path / "database_url"
    value = "postgresql+psycopg://user:private_password@database/db"
    path.write_text(value + "\n")
    monkeypatch.setenv("AGROCAST_DATABASE_URL_FILE", str(path))
    monkeypatch.setenv("AGROCAST_PUBLIC_ORIGIN", "https://testserver")
    settings = IdentitySettings.from_environment()
    assert settings.database_url == value
    assert value not in repr(settings)


@pytest.mark.parametrize("value", ["sqlite:///test.db", "postgresql://user@db/test", "invalid", "x" * 4097])
def test_invalid_database_file_does_not_open_http_access(tmp_path, monkeypatch, caplog, value):
    path = tmp_path / "database_url"
    path.write_text(value)
    monkeypatch.setenv("AGROCAST_DATABASE_URL_FILE", str(path))
    monkeypatch.setenv("AGROCAST_PUBLIC_ORIGIN", "https://testserver")
    with pytest.raises(ConfigurationError):
        with TestClient(create_app()):
            pytest.fail("invalid configuration must prevent startup")
    assert value not in caplog.text


def test_missing_schema_cannot_create_identity_automatically(tmp_path):
    engine = create_engine("sqlite:///" + str(tmp_path / "empty.db"))
    try:
        with pytest.raises(Exception):
            IdentityService(engine, "https://testserver")
        assert not inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_secret_generation_is_exclusive_private_and_not_printed(tmp_path, capsys):
    cli.init_secrets(tmp_path, "database")
    password = (tmp_path / "database_password").read_text().strip()
    url = (tmp_path / "database_url").read_text().strip()
    assert len(password) >= 32
    assert password in url
    assert (tmp_path / "database_password").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "database_url").stat().st_mode & 0o777 == 0o600
    assert password not in capsys.readouterr().out
    with pytest.raises(FileExistsError):
        cli.init_secrets(tmp_path, "database")
    assert (tmp_path / "database_password").read_text().strip() == password
    assert (tmp_path / "database_url").read_text().strip() == url


def test_secret_generation_removes_only_its_own_partial_files(tmp_path):
    (tmp_path / "database_url").write_text("existing")
    with pytest.raises(FileExistsError):
        cli.init_secrets(tmp_path, "database")
    assert not (tmp_path / "database_password").exists()
    assert (tmp_path / "database_url").read_text() == "existing"


def test_bootstrap_is_an_operator_cli_not_an_open_http_registration(identity_engine, account_password, monkeypatch, capsys):
    class Settings:
        public_origin = "https://testserver"
        session_seconds = 28800

        def engine(self):
            return identity_engine

    monkeypatch.setattr(cli.IdentitySettings, "from_environment", lambda: Settings())
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: account_password)
    cli.main(["bootstrap", "--organization", "New organization", "--username", "first_admin"])
    output = capsys.readouterr().out
    assert "first_admin" in output
    assert account_password not in output
    assert "password_hash" not in output


def test_bootstrap_does_not_overwrite_existing_identity(identity, account_password):
    with pytest.raises(IdentityError) as error:
        identity.bootstrap("Another organization", "operator_a", account_password)
    assert error.value.status == 409
    assert identity.login("operator_a", account_password).principal.role.value == "operator"


def test_real_postgresql_factory_reads_settings_and_authenticates_existing_users(identity, identity_engine, account_password, tmp_path, monkeypatch):
    if identity_engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL factory integration test")
    from fastapi.testclient import TestClient
    from sqlalchemy import text
    from agrocast.serve.product import create_app

    with identity_engine.connect() as connection:
        schema = connection.execute(text("SELECT current_schema()")).scalar_one()
    url = identity_engine.url.update_query_dict({"options": "-csearch_path=" + schema})
    path = tmp_path / "database_url"
    path.write_text(url.render_as_string(hide_password=False))
    monkeypatch.setenv("AGROCAST_DATABASE_URL_FILE", str(path))
    monkeypatch.setenv("AGROCAST_PUBLIC_ORIGIN", "https://testserver")
    monkeypatch.setenv("AGROCAST_STATE_DIR", str(tmp_path / "state"))
    application = create_app()
    assert application.state.identity is None
    with TestClient(application, base_url="https://testserver") as client:
        assert application.state.identity is not None
        response = client.post("/api/auth/login", headers={"Origin": "https://testserver"}, json={"username": "operator_a", "password": account_password})
        assert response.status_code == 200
        assert client.get("/api/fields").status_code == 200
    assert application.state.identity is None
    assert not application.state.started
