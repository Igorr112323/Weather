import hashlib
import os
import re
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import urlsplit

from agrocast.core.jsoncodec import canonical_json

BASE = Path(__file__).resolve().parents[2]
DEFAULT_WORLD = BASE / "world"
DEFAULT_STATE = BASE / "data"


class ConfigurationError(ValueError):
    pass


def validate_origin(value):
    try:
        parts = urlsplit(value)
        valid = value.isascii() and not any(character.isspace() for character in value) and parts.scheme == "https" and parts.hostname and not (parts.username or parts.password or parts.path or parts.query or parts.fragment or "*" in parts.netloc)
        parts.port
    except ValueError:
        valid = False
    if not valid:
        raise ConfigurationError("AGROCAST_PUBLIC_ORIGIN must be an exact HTTPS origin without a path")
    return value


def _option(environment, name, aliases=(), default=None):
    values = [environment[key] for key in (name, *aliases) if key in environment]
    if values and any(value != values[0] for value in values):
        raise ConfigurationError(f"Conflicting values for {name} and its legacy aliases")
    return values[0] if values else default


def _path(value, name):
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        raise ConfigurationError(f"{name} must be an absolute path")
    return path.resolve()


def _boolean(value, name):
    if value is None:
        return None
    if value not in {"0", "1", "false", "true"}:
        raise ConfigurationError(f"{name} must be 0, 1, false or true")
    return value in {"1", "true"}


@dataclass(frozen=True)
class RuntimeSettings:
    world_dir: Path = DEFAULT_WORLD
    state_dir: Path = DEFAULT_STATE
    config_file: Path | None = None
    database_url_file: Path | None = field(default=None, repr=False)
    public_origin: str | None = None
    release_manifest_file: Path | None = None
    log_level: str = "INFO"
    session_seconds: int = 28800
    phys_preset: str | None = None
    regime_guard: bool | None = None
    ospr_enabled: bool | None = None
    ospr_weight: float | None = None

    def __post_init__(self):
        for name in ("world_dir", "state_dir", "config_file", "database_url_file", "release_manifest_file"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _path(value, name))
        if self.world_dir is None or self.state_dir is None:
            raise ConfigurationError("world_dir and state_dir are required")
        if self.world_dir == self.state_dir or self.world_dir in self.state_dir.parents or self.state_dir in self.world_dir.parents:
            raise ConfigurationError("Bundle and writable state directories must not overlap")
        if self.log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigurationError("AGROCAST_LOG_LEVEL is invalid")
        if type(self.session_seconds) is not int or not 300 <= self.session_seconds <= 86400:
            raise ConfigurationError("AGROCAST_SESSION_SECONDS must be between 300 and 86400")
        if self.public_origin is not None:
            validate_origin(self.public_origin)
        if self.phys_preset not in {None, "land", "state", "land_d6"}:
            raise ConfigurationError("AGROCAST_PHYS_PRESET is invalid")
        for name in ("regime_guard", "ospr_enabled"):
            if getattr(self, name) is not None and type(getattr(self, name)) is not bool:
                raise ConfigurationError(f"{name} must be boolean")
        if self.ospr_weight is not None and (type(self.ospr_weight) not in (int, float) or not 0 <= self.ospr_weight <= 1):
            raise ConfigurationError("AGROCAST_OSPR_W must be finite and between 0 and 1")

    @classmethod
    def from_environment(cls, environment=None):
        env = os.environ if environment is None else environment
        seconds = env.get("AGROCAST_SESSION_SECONDS", "28800")
        if not re.fullmatch(r"[0-9]+", seconds):
            raise ConfigurationError("AGROCAST_SESSION_SECONDS must be an integer")
        try:
            weight = float(env["AGROCAST_OSPR_W"]) if "AGROCAST_OSPR_W" in env else None
        except ValueError:
            raise ConfigurationError("AGROCAST_OSPR_W must be numeric") from None
        return cls(
            world_dir=_option(env, "AGROCAST_WORLD_DIR", ("AGROCAST_WORLD",), str(DEFAULT_WORLD)),
            state_dir=_option(env, "AGROCAST_STATE_DIR", ("AGROCAST_DATA", "AGROCAST_DATA_DIR"), str(DEFAULT_STATE)),
            config_file=env.get("AGROCAST_CONFIG_FILE"), database_url_file=env.get("AGROCAST_DATABASE_URL_FILE"),
            public_origin=env.get("AGROCAST_PUBLIC_ORIGIN"), release_manifest_file=env.get("AGROCAST_RELEASE_MANIFEST_FILE"),
            log_level=env.get("AGROCAST_LOG_LEVEL", "INFO"), session_seconds=int(seconds),
            phys_preset=_option(env, "AGROCAST_PHYS_PRESET", ("PHYS_PRESET",)),
            regime_guard=_boolean(_option(env, "AGROCAST_REGIME_GUARD", ("REGIME_GUARD",)), "AGROCAST_REGIME_GUARD"),
            ospr_enabled=_boolean(env.get("AGROCAST_OSPR"), "AGROCAST_OSPR"), ospr_weight=weight,
        )

    def with_paths(self, world_dir=None, state_dir=None):
        return replace(self, world_dir=Path(world_dir).resolve() if world_dir is not None else self.world_dir, state_dir=Path(state_dir).resolve() if state_dir is not None else self.state_dir)

    def writable_path(self, path):
        path = Path(path).resolve()
        if path == self.world_dir or self.world_dir in path.parents:
            raise ConfigurationError("Cannot write into the read-only bundle")
        return path

    def numeric_overrides(self):
        return {key: getattr(self, key) for key in ("phys_preset", "regime_guard", "ospr_enabled", "ospr_weight") if getattr(self, key) is not None}

    def compute_config(self):
        from agrocast.core.config import Config

        if not self.world_dir.is_dir():
            raise ConfigurationError("AGROCAST_WORLD_DIR must contain a readable scientific bundle")
        if self.release_manifest_file is not None:
            from agrocast.store.results import Releases

            try:
                Releases.from_file(self.release_manifest_file)
            except (OSError, ValueError, TypeError):
                raise ConfigurationError("AGROCAST_RELEASE_MANIFEST_FILE is missing or invalid") from None
        source = self.config_file or self.world_dir / "config.json"
        try:
            config = Config.load(source)
        except (OSError, ValueError, TypeError, KeyError):
            raise ConfigurationError("Scientific configuration is missing or invalid") from None
        config.data_dir = str(self.state_dir / "compute")
        config.runtime_dir = str(self.state_dir)
        config.bundle_dir = str(self.world_dir)
        config.shared_zarr = str(self.world_dir / "zarr")
        config.use_bundle_models = True
        for key, value in self.numeric_overrides().items():
            setattr(config, key, value)
        try:
            config.validate()
        except ValueError:
            raise ConfigurationError("Effective scientific configuration is invalid") from None
        return config

    def prepare_state(self):
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.TemporaryFile(dir=self.state_dir) as handle:
                handle.write(b"ready")
            for name in ("compute", "backups", "migration", "offline-jobs", "results-v1"):
                target = self.state_dir / name
                if target.is_symlink() or self.world_dir == target.resolve() or self.world_dir in target.resolve().parents:
                    raise ConfigurationError("Writable state must not point into the bundle")
                target.mkdir(exist_ok=True, mode=0o700)
        except OSError:
            raise ConfigurationError("AGROCAST_STATE_DIR is not writable") from None

    def identity_settings(self):
        from sqlalchemy.engine import make_url
        from agrocast.identity.database import IdentitySettings

        if self.database_url_file is None:
            raise ConfigurationError("AGROCAST_DATABASE_URL_FILE is required")
        if self.public_origin is None:
            raise ConfigurationError("AGROCAST_PUBLIC_ORIGIN is required")
        try:
            with self.database_url_file.open(encoding="utf-8") as handle:
                value = handle.read(4097).strip()
            if not value or len(value) > 4096 or "\n" in value or "\r" in value or make_url(value).drivername != "postgresql+psycopg":
                raise ValueError("invalid DSN")
        except Exception:
            raise ConfigurationError("AGROCAST_DATABASE_URL_FILE must contain one valid PostgreSQL psycopg DSN") from None
        return IdentitySettings(value, self.public_origin, self.session_seconds)

    def public_snapshot(self):
        config = self.compute_config().to_dict()
        for key in ("data_dir", "shared_zarr", "bundle_dir", "runtime_dir"):
            config.pop(key, None)
        return {
            "world_dir": str(self.world_dir), "state_dir": str(self.state_dir),
            "config_file": str(self.config_file or self.world_dir / "config.json"),
            "public_origin": self.public_origin, "session_seconds": self.session_seconds,
            "log_level": self.log_level, "numerics": config,
            "release_manifest_file": str(self.release_manifest_file) if self.release_manifest_file else None,
        }

    def fingerprint(self):
        return hashlib.sha256(canonical_json(self.public_snapshot()).encode()).hexdigest()
