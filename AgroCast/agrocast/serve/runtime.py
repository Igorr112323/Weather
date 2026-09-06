import logging
from contextlib import asynccontextmanager
from uuid import uuid4

from agrocast.core.settings import ConfigurationError
from agrocast.identity.credentials import passwords
from agrocast.identity.service import IdentityService
from agrocast.queue.service import JobQueue


def runtime_lifespan(settings, injected=None):
    @asynccontextmanager
    async def lifespan(application):
        engine = None
        handler = None
        logger = logging.getLogger("agrocast.runtime." + uuid4().hex)
        try:
            configuration = settings.compute_config()
            if injected is not None:
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
