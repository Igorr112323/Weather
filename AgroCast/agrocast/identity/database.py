from dataclasses import dataclass, field
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from agrocast.identity.schema import REVISION
from agrocast.core.settings import RuntimeSettings, validate_origin


@dataclass(frozen=True)
class IdentitySettings:
    database_url: str = field(repr=False)
    public_origin: str
    session_seconds: int = 28800

    def __post_init__(self):
        validate_origin(self.public_origin)
        if not 300 <= self.session_seconds <= 86400:
            raise ValueError("Session lifetime must be between 300 and 86400 seconds")

    @classmethod
    def from_environment(cls):
        settings = RuntimeSettings.from_environment()
        return settings.identity_settings() if settings.database_url_file is not None else None

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
