import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url

from agrocast.identity.schema import REVISION


@dataclass(frozen=True)
class IdentitySettings:
    database_url: str = field(repr=False)
    public_origin: str
    session_seconds: int = 28800

    def __post_init__(self):
        parts = urlsplit(self.public_origin)
        if (
            parts.scheme != "https" or not parts.hostname or parts.username or parts.password
            or parts.path or parts.query or parts.fragment or "*" in parts.netloc
        ):
            raise ValueError("AGROCAST_PUBLIC_ORIGIN must be an HTTPS origin without a path")
        parts.port
        if not 300 <= self.session_seconds <= 86400:
            raise ValueError("Session lifetime must be between 300 and 86400 seconds")

    @classmethod
    def from_environment(cls):
        path = os.environ.get("AGROCAST_DATABASE_URL_FILE")
        if not path:
            return None
        with Path(path).open(encoding="utf-8") as handle:
            url = handle.read(4097).strip()
        if not url or len(url) > 4096 or "\n" in url or "\r" in url:
            raise ValueError("Invalid database configuration")
        if make_url(url).drivername != "postgresql+psycopg":
            raise ValueError("HTTP deployment requires PostgreSQL with psycopg")
        return cls(url, os.environ.get("AGROCAST_PUBLIC_ORIGIN", ""), int(os.environ.get("AGROCAST_SESSION_SECONDS", "28800")))

    def engine(self):
        return create_engine(
            self.database_url, pool_pre_ping=True, pool_size=5, max_overflow=0,
            pool_timeout=3, hide_parameters=True, connect_args={"connect_timeout": 5},
        )


def migrate(engine: Engine, revision: str = "head"):
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[2] / "migrations"))
    with engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            connection.execute(text("SELECT pg_advisory_xact_lock(482076011)"))
        config.attributes["connection"] = connection
        command.upgrade(config, revision)


def check_schema(engine: Engine):
    with engine.connect() as connection:
        version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        if version != REVISION:
            raise ValueError("Identity schema needs migration")
