import logging
import time
from contextlib import asynccontextmanager
from uuid import NAMESPACE_URL, uuid4, uuid5

from agrocast.core.settings import ConfigurationError
from agrocast.identity.credentials import Principal, Role, passwords
from agrocast.identity.service import IdentityService
from agrocast.queue.service import JobQueue

DESKTOP_ORIGIN = "https://127.0.0.1"
LOCAL_USER_ID = str(uuid5(NAMESPACE_URL, "agrocast-desktop-local"))
LOCAL_ORG_ID = str(uuid5(NAMESPACE_URL, "agrocast-desktop-organization"))


def _desktop_engine(settings):
    from sqlalchemy import create_engine, event, insert, select

    from agrocast.identity.database import migrate
    from agrocast.identity.schema import organizations, users

    engine = create_engine("sqlite:///" + str(settings.state_dir / "agrocast.db"), hide_parameters=True)

    @event.listens_for(engine, "connect")
    def _configure(dbapi_connection, record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")
        dbapi_connection.execute("PRAGMA busy_timeout=5000")
        dbapi_connection.execute("PRAGMA journal_mode=WAL")

    migrate(engine)
    now = int(time.time())
    with engine.begin() as connection:
        if connection.execute(select(users.c.id)).first() is None:
            connection.execute(insert(organizations).values(id=LOCAL_ORG_ID, name="Локальная организация", active=True, created_at=now))
            connection.execute(insert(users).values(id=LOCAL_USER_ID, organization_id=LOCAL_ORG_ID, username="local", password_hash=passwords.dummy_hash, role="admin", active=True, created_at=now))
    return engine


def _desktop_principal():
    return Principal(id=LOCAL_USER_ID, organization_id=LOCAL_ORG_ID, username="local", role=Role.ADMIN, session_hash="desktop", csrf_token="0" * 43)


def runtime_lifespan(settings, injected=None):
    @asynccontextmanager
    async def lifespan(application):
        engine = None
        handler = None
        logger = logging.getLogger("agrocast.runtime." + uuid4().hex)
        try:
            configuration = settings.compute_config()
            if settings.desktop_mode:
                settings.prepare_state()
                engine = _desktop_engine(settings)
                identity = IdentityService(engine, settings.public_origin or DESKTOP_ORIGIN, settings.session_seconds)
                application.state.desktop_principal = _desktop_principal()
            elif injected is not None:
                if settings.public_origin != injected.public_origin or settings.session_seconds != injected.session_seconds:
                    raise ConfigurationError("Injected identity must use the configured origin and session lifetime")
                identity = injected
            else:
                options = settings.identity_settings()
                try:
                    engine = options.engine()
                    identity = IdentityService(engine, options.public_origin, options.session_seconds)
                except Exception:
                    raise ConfigurationError("PostgreSQL is unavailable or the schema needs migration") from None
            settings.prepare_state()
            passwords.dummy_hash
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s :: %(message)s"))
            logger.setLevel(settings.log_level)
            logger.propagate = False
            logger.addHandler(handler)
            application.state.config = configuration
            application.state.identity = identity
            application.state.queue = JobQueue(identity.engine, settings, identity)
            application.state.logger = logger
            application.state.started = True
        except Exception:
            if engine is not None:
                engine.dispose()
            raise
        try:
            yield
        finally:
            application.state.started = False
            if injected is None:
                application.state.identity = None
            if engine is not None:
                engine.dispose()
            if handler is not None:
                logger.removeHandler(handler)
                handler.close()
    return lifespan
