from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def compose():
    return yaml.safe_load((ROOT / "deploy" / "docker-compose.yml").read_text())


def test_only_caddy_has_host_ports(compose):
    for name, service in compose["services"].items():
        if name != "caddy":
            assert "ports" not in service
    assert compose["services"]["app"]["expose"] == ["8501"]
    assert set(compose["services"]["caddy"]["ports"]) == {"80:80", "443:443"}


def test_runtime_and_migrations_use_secret_files(compose):
    for name in ("app", "migrate"):
        service = compose["services"][name]
        assert service["secrets"] == ["database_url"]
        assert service["environment"]["AGROCAST_DATABASE_URL_FILE"] == "/run/secrets/database_url"
        assert service["environment"]["AGROCAST_PUBLIC_ORIGIN"].startswith("https://${AGROCAST_DOMAIN:?")
        assert "AGROCAST_PILOT_SECRET_FILE" not in service["environment"]
    database = compose["services"]["database"]
    assert database["environment"]["POSTGRES_PASSWORD_FILE"] == "/run/secrets/database_password"
    assert "POSTGRES_PASSWORD" not in database["environment"]
    assert compose["secrets"]["database_url"]["file"].startswith("${AGROCAST_DATABASE_URL_FILE:?")
    assert compose["secrets"]["database_password"]["file"].startswith("${AGROCAST_DB_PASSWORD_FILE:?")


def test_migrations_gate_http_startup(compose):
    assert compose["services"]["app"]["depends_on"]["migrate"]["condition"] == "service_completed_successfully"
    assert compose["services"]["migrate"]["depends_on"]["database"]["condition"] == "service_healthy"
    assert compose["services"]["migrate"]["command"] == ["python", "-m", "agrocast.identity.cli", "migrate"]


def test_compose_volume_shorthand_is_valid_and_state_is_persistent(compose):
    for service in compose["services"].values():
        for volume in service.get("volumes", []):
            assert isinstance(volume, str)
            source, target, *options = volume.split(":")
            assert source and target.startswith("/")
            assert not options or options == ["ro"]
    assert "agrocast-db:/var/lib/postgresql/data" in compose["services"]["database"]["volumes"]
    assert "agrocast-data:/app/data" in compose["services"]["app"]["volumes"]
    assert "./Caddyfile:/etc/caddy/Caddyfile:ro" in compose["services"]["caddy"]["volumes"]
    assert {"agrocast-db", "agrocast-data", "caddy-data", "caddy-config", "caddy-logs"} <= set(compose["volumes"])


def test_caddy_does_not_log_credentials_or_inject_user_identity():
    caddyfile = (ROOT / "deploy" / "Caddyfile").read_text()
    assert "output file /var/log/caddy/access.log" in caddyfile
    assert "reverse_proxy app:8501" in caddyfile
    for value in ("log_credentials", "X-Auth-Request-User", "X-Forwarded-User"):
        assert value not in caddyfile


def test_container_healthcheck_uses_only_public_liveness():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "http://127.0.0.1:8501/health/live" in dockerfile
    assert "/api/health" not in dockerfile
    assert "--host" in dockerfile and "0.0.0.0" in dockerfile


def test_serve_cli_uses_the_guarded_product_without_opening_offline_config(monkeypatch):
    import uvicorn
    from agrocast.serve import cli, product

    run = Mock()
    load = Mock(side_effect=AssertionError("HTTP startup must not open offline configuration"))
    monkeypatch.setattr(uvicorn, "run", run)
    monkeypatch.setattr(cli, "_load_config", load)
    cli.main(["serve", "--port", "8501"])
    run.assert_called_once_with(product.app, host="0.0.0.0", port=8501)
    load.assert_not_called()
