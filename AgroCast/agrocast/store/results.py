import hashlib
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, StrictInt, model_validator

from agrocast.core.contracts import (
    Contract, Digest, ForecastKind, ForecastMode, ForecastSpec, Horizon, Latitude, Longitude,
    RegionFieldSpec, RegionId, Revision, Variable, YearMonth,
)
from agrocast.core.jsoncodec import canonical_json, strict_json

MAX_ENTRY_BYTES = 16 * 1024 * 1024


def fingerprint(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class Releases(Contract):
    model_config = ConfigDict(extra="forbid", frozen=True)
    data_release: Digest
    model_release: Digest
    application_release: Digest

    @classmethod
    def from_file(cls, path=None):
        from agrocast.core.settings import RuntimeSettings

        path = path or RuntimeSettings.from_environment().release_manifest_file
        if not path:
            raise ValueError("Explicit data/model/application release identity is required")
        with Path(path).open(encoding="utf-8") as handle:
            source = handle.read(65537)
        if len(source) > 65536:
            raise ValueError("Release identity file is too large")
        return cls.model_validate(strict_json(source))


class VarietySnapshot(Contract):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: UUID
    revision: Revision
    content_sha256: Digest


class CacheScope(Contract):
    model_config = ConfigDict(extra="forbid", frozen=True)
    namespace: Literal["owned", "offline_research"]
    organization_id: UUID | None = None
    owner_id: UUID | None = None

    @model_validator(mode="after")
    def ownership(self):
        if self.namespace == "owned" and (self.owner_id is None or self.organization_id is None):
            raise ValueError("Owned cache entries require owner and organization")
        if self.namespace == "offline_research" and (self.owner_id is not None or self.organization_id is not None):
            raise ValueError("Offline cache is not an ownership fallback")
        return self

    @classmethod
    def for_principal(cls, principal):
        return cls(namespace="owned", owner_id=principal.id, organization_id=principal.organization_id)


class ResultIdentity(Contract):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    cache_version: Literal["result-v1"] = "result-v1"
    operation: Literal["point", "region"]
    scope: CacheScope
    region: RegionId
    lat: Latitude | None = None
    lon: Longitude | None = None
    point_id: str | None = None
    grid_sha256: Digest | None = None
    start: YearMonth
    horizon: Horizon
    mode: ForecastMode
    season_len: StrictInt
    kind: ForecastKind
    variables: tuple[Variable, ...]
    variety: VarietySnapshot | None = None
    releases: Releases
    settings_sha256: Digest

    @model_validator(mode="after")
    def target(self):
        if self.operation == "point":
            if self.lat is None or self.lon is None or self.grid_sha256 is not None:
                raise ValueError("Point identity requires exact coordinates")
        elif self.grid_sha256 is None or self.lat is not None or self.lon is not None or self.point_id is not None:
            raise ValueError("Regional identity requires a grid fingerprint")
        parameters = {"start": self.start, "horizon": self.horizon, "mode": self.mode, "season_len": self.season_len, "variables": self.variables, "region": self.region}
        if self.operation == "point":
            ForecastSpec(**parameters, lat=self.lat, lon=self.lon, point_id=self.point_id, kind=self.kind)
        else:
            RegionFieldSpec(**parameters)
            if self.kind != ForecastKind.FORECAST or self.variety is not None:
                raise ValueError("regional cache does not support this computation")
        return self

    def canonical(self):
        return canonical_json(self.model_dump(mode="json"))

    def key(self):
        return hashlib.sha256(self.canonical().encode("utf-8")).hexdigest()

    @classmethod
    def point(cls, request: ForecastSpec, releases: Releases, settings, scope: CacheScope, variety=None):
        if request.variety_id is not None:
            if variety is None or variety.id != request.variety_id or variety.revision != request.variety_revision:
                raise ValueError("Resolved variety snapshot does not match the request")
        elif variety is not None:
            raise ValueError("Unexpected variety snapshot")
        return cls(
            operation="point", scope=scope, region=request.region,
            lat=0.0 if request.lat == 0 else request.lat, lon=0.0 if request.lon == 0 else request.lon,
            point_id=request.point_id, start=request.start, horizon=request.horizon, mode=request.mode,
            season_len=request.season_len, kind=request.kind, variables=tuple(sorted(request.variables)),
            variety=variety, releases=releases, settings_sha256=fingerprint(settings),
        )

    @classmethod
    def regional(cls, request: RegionFieldSpec, releases: Releases, settings, grid):
        return cls(
            operation="region", scope=CacheScope(namespace="offline_research"), region=request.region,
            grid_sha256=fingerprint(grid), start=request.start, horizon=request.horizon, mode=request.mode,
            season_len=request.season_len, kind=ForecastKind.FORECAST, variables=tuple(sorted(request.variables)),
            releases=releases, settings_sha256=fingerprint(settings),
        )


@dataclass(frozen=True)
class CacheHit:
    payload: dict
    created_at: int
    age_s: int


class ResultCache:
    def __init__(self, data_root, clock=time.time):
        self.root = Path(data_root) / "results-v1"
        self.clock = clock

    def path(self, identity: ResultIdentity):
        return self.root / (identity.key() + ".json")

    def read(self, identity: ResultIdentity, max_age=None):
        if max_age is not None and (type(max_age) not in (int, float) or not 0 <= max_age < float("inf")):
            raise ValueError("Invalid cache TTL")
        path = self.path(identity)
        try:
            with path.open("rb") as handle:
                source = handle.read(MAX_ENTRY_BYTES + 1)
            if len(source) > MAX_ENTRY_BYTES:
                return None
            value = strict_json(source)
            if not isinstance(value, dict) or value.get("state") != "complete":
                return None
            if value.get("key") != identity.key() or canonical_json(value.get("identity")) != identity.canonical():
                return None
            created = value.get("created_at")
            if type(created) is not int or created < 0 or created > int(self.clock()):
                return None
            age = int(self.clock()) - created
            if max_age is not None and age >= max_age:
                return None
            payload = value.get("payload")
            if not isinstance(payload, dict) or value.get("payload_sha256") != fingerprint(payload):
                return None
            return CacheHit(payload, created, age)
        except (OSError, ValueError, TypeError, RecursionError):
            return None

    def write(self, identity: ResultIdentity, payload):
        if not isinstance(payload, dict):
            raise ValueError("Result payload must be an object")
        envelope = {
            "state": "complete", "key": identity.key(), "identity": identity.model_dump(mode="json"),
            "created_at": int(self.clock()), "payload_sha256": fingerprint(payload), "payload": payload,
        }
        data = canonical_json(envelope).encode("utf-8")
        if len(data) > MAX_ENTRY_BYTES:
            raise ValueError("Result cache entry is too large")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.root, prefix="." + identity.key() + ".", suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path(identity))
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return self.path(identity)
