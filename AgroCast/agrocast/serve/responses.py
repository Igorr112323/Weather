import json
from typing import Annotated, Generic, Literal, TypeVar
from uuid import UUID

from pydantic import Field, JsonValue, StrictBool, StrictInt, field_validator, model_validator

from agrocast.core.contracts import Contract, ForecastMode, JobStatus, Latitude, Longitude, RegionId, Revision
from agrocast.identity.credentials import Role
from agrocast.serve.account_models import CropBody, FieldBody, SubscriptionBody

T = TypeVar("T")
Timestamp = Annotated[StrictInt, Field(ge=0)]


class UserPublic(Contract):
    id: UUID
    organization_id: UUID
    username: str
    role: Role


class UserAccount(UserPublic):
    active: StrictBool


class SessionResponse(Contract):
    user: UserPublic
    csrf_token: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{43}$")]


class LoginResponse(SessionResponse):
    expires_at: Timestamp


class UserResponse(Contract):
    user: UserAccount


class UsersResponse(Contract):
    users: list[UserAccount]


class IdentityEvent(Contract):
    id: UUID
    organization_id: UUID
    actor_id: UUID
    action: str
    subject_id: UUID
    created_at: Timestamp


class EventsResponse(Contract):
    events: list[IdentityEvent]


class OwnedRecord(Contract, Generic[T]):
    id: UUID
    organization_id: UUID
    owner_id: UUID
    data: T
    created_at: Timestamp
    updated_at: Timestamp


class FieldRecord(OwnedRecord[FieldBody]):
    pass


class CropRecord(OwnedRecord[CropBody]):
    revision: Revision


class SubscriptionRecord(OwnedRecord[SubscriptionBody]):
    field_id: UUID


class JobRecord(OwnedRecord[dict[str, JsonValue]]):
    status: JobStatus

    @field_validator("data")
    @classmethod
    def finite_json(cls, value):
        json.dumps(value, allow_nan=False)
        return value


class FieldResponse(Contract):
    field: FieldRecord


class FieldsResponse(Contract):
    fields: list[FieldRecord]


class CropResponse(Contract):
    crop: CropRecord


class CropsResponse(Contract):
    crops: list[CropRecord]


class SubscriptionResponse(Contract):
    subscription: SubscriptionRecord


class SubscriptionsResponse(Contract):
    subscriptions: list[SubscriptionRecord]


class HistoricalValidation(Contract):
    status: Literal["unverified"]
    production_ready: Literal[False]
    agronomic_use_allowed: Literal[False]
    coverage_guaranteed: Literal[False]
    audit_date: str
    blocking_findings: list[str]
    warning: str


class Historical(Contract):
    policy_version: str
    validation: HistoricalValidation


class JobsResponse(Historical):
    jobs: list[JobRecord]


class JobResponse(Historical):
    job: JobRecord


class PublicationRecord(OwnedRecord[dict[str, JsonValue]]):
    job_id: UUID
    checksum: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

    @field_validator("data")
    @classmethod
    def finite_json(cls, value):
        json.dumps(value, allow_nan=False)
        return value


class PublicationsResponse(Historical):
    publications: list[PublicationRecord]


class PublicationResponse(Historical):
    publication: PublicationRecord


class AcceptedJob(Contract):
    job: UUID
    status: Literal["queued"] = "queued"
    status_url: Annotated[str, Field(pattern=r"^/api/jobs/[0-9a-f-]{36}$")]

    @model_validator(mode="after")
    def matching_location(self):
        if self.status_url != f"/api/jobs/{self.job}":
            raise ValueError("status URL must identify the accepted job")
        return self


class LivenessResponse(Contract):
    status: Literal["alive"]
    stage: Literal["closed_pilot"]
    forecast_enabled: Literal[False]


class DiagnosticResponse(Contract):
    ok: Literal[True]
    world: str
    daily_region: str | None = None
    sst: str | None = None
    fields_monthly: str | None = None
    strat_snow: str | None = None
    oisst_boxes: str | None = None
    regimes: str | None = None


class PointInfo(Contract):
    id: str
    lat: Latitude
    lon: Longitude


class GridCell(PointInfo):
    coverage: float
    coverage_t2m: float
    coverage_tp: float


class Bounds(Contract):
    lat_min: Latitude
    lat_max: Latitude
    lon_min: Longitude
    lon_max: Longitude


class GridArtifact(Contract):
    name: str
    cell_deg: float
    min_coverage: float
    bounds: Bounds
    n_candidates: Annotated[StrictInt, Field(ge=0)]
    n_cells: Annotated[StrictInt, Field(ge=0)]
    cells: list[GridCell]
    region: RegionId
    region_name: str


class GridResponse(Historical):
    ok: Literal[True]
    grid: GridArtifact


class SkillResponse(Historical):
    ok: Literal[True]
    skill: dict[str, JsonValue]


class ValueResponse(Historical):
    ok: Literal[True]
    report: dict[str, JsonValue]


class RegionSummary(Contract):
    region: RegionId
    name: str
    bounds: Bounds
    built: StrictBool
    n_candidates: StrictInt | None = None
    n_cells: StrictInt | None = None
    skill: StrictBool | None = None
    verifications: StrictInt | None = None
    seasonal_t2m_rpss: float | None = None
    seasonal_t2m_hit: float | None = None


class RegionsResponse(Historical):
    ok: Literal[True]
    regions: list[RegionSummary]


class AccessInfo(Contract):
    authentication: Literal["server_session"]
    roles: list[Role]
    read_only: StrictBool
    ownership_required: Literal[True]


class CropCapability(Contract):
    id: Literal["maize"]
    name: str


class RegionCapability(Contract):
    id: Literal[RegionId.KRAI]
    name: str
    inspection_points: list[PointInfo]


class HistoricalCapabilities(Contract):
    enabled: StrictBool
    purpose: Literal["inspection_only"]
    years: list[StrictInt]
    modes: list[ForecastMode]
    leads: list[StrictInt]
    season_len: StrictInt


class ForecastCapabilities(Contract):
    enabled: Literal[False]
    modes: list[ForecastMode]
    horizons: list[StrictInt]
    season_lengths: list[StrictInt]


class Operations(Contract):
    hindcast: Literal[False]
    region_refresh: Literal[False]
    crop_mutation: StrictBool
    field_management: StrictBool
    subscription_preferences: StrictBool
    subscriptions: Literal[False]
    job_access: StrictBool
    agro_recommendations: Literal[False]
    legacy_api: Literal[False]


class CapabilitiesResponse(Historical):
    stage: Literal["closed_pilot"]
    access: AccessInfo
    crops: list[CropCapability]
    regions: list[RegionCapability]
    historical_results: HistoricalCapabilities
    forecast: ForecastCapabilities
    operations: Operations
    permissions: dict[Role, list[str]]


class RegionFieldResponse(Historical):
    ok: Literal[True]
    cache_key: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    meta: dict[str, JsonValue]
    points: list[dict[str, JsonValue]]
    field: dict[str, list[list[float]]]


def accepted_job_response(job_id):
    from starlette.responses import JSONResponse

    job_id = UUID(str(job_id))
    body = AcceptedJob(job=job_id, status_url=f"/api/jobs/{job_id}")
    return JSONResponse(body.model_dump(mode="json"), status_code=202, headers={"Location": body.status_url, "Cache-Control": "no-store"})
