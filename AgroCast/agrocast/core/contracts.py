import re
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StringConstraints, field_validator, model_validator

CONTRACT_VERSION = "api-v1"
YearMonth = Annotated[str, StringConstraints(strict=True, pattern=r"^(19|20)[0-9]{2}-(0[1-9]|1[0-2])$")]
Latitude = Annotated[StrictFloat, Field(ge=-90, le=90, allow_inf_nan=False)]
Longitude = Annotated[StrictFloat, Field(ge=-180, le=180, allow_inf_nan=False)]
Horizon = Annotated[StrictInt, Field(ge=1, le=6)]
Month = Annotated[StrictInt, Field(ge=1, le=12)]
Revision = Annotated[StrictInt, Field(ge=1)]
Digest = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]


class ForecastMode(StrEnum):
    MONTHLY = "monthly"
    SEASONAL = "seasonal"


class ForecastKind(StrEnum):
    FORECAST = "forecast"
    HINDCAST = "hindcast"


class Variable(StrEnum):
    T2M = "t2m"
    TP = "tp"


class RegionId(StrEnum):
    KRAI = "krai"
    STAVROPOL = "stavropol"
    ROSTOV = "rostov"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, validate_default=True)


class Coordinates(Contract):
    lat: Latitude
    lon: Longitude


class Timing(Contract):
    horizon: Horizon = 3
    mode: ForecastMode = ForecastMode.SEASONAL
    season_len: Annotated[StrictInt, Field(ge=1, le=6, description="Default: 1 for monthly, 3 for seasonal")] = 3
    variables: Annotated[tuple[Variable, ...], Field(min_length=1, max_length=2)] = (Variable.T2M, Variable.TP)

    @model_validator(mode="before")
    @classmethod
    def default_length(cls, value):
        if isinstance(value, dict) and "season_len" not in value and value.get("mode") == "monthly":
            return {**value, "season_len": 1}
        return value

    @model_validator(mode="after")
    def consistent_mode(self):
        if self.season_len != (1 if self.mode == ForecastMode.MONTHLY else 3):
            raise ValueError("season_len must be 1 for monthly or 3 for seasonal")
        if len(set(self.variables)) != len(self.variables):
            raise ValueError("variables must not contain duplicates")
        return self


class VarietySelection(Contract):
    variety_id: UUID | None = None
    variety_revision: Revision | None = None

    @model_validator(mode="after")
    def paired_variety(self):
        if (self.variety_id is None) != (self.variety_revision is None):
            raise ValueError("variety_id and variety_revision must be provided together")
        return self


class ForecastSpec(Coordinates, Timing, VarietySelection):
    start: YearMonth
    kind: ForecastKind = ForecastKind.FORECAST
    region: RegionId = RegionId.KRAI
    point_id: Annotated[str, StringConstraints(strict=True, pattern=r"^P[0-9]{2}$")] | None = None

    @model_validator(mode="after")
    def complete_periods(self):
        if self.horizon % self.season_len:
            raise ValueError("horizon must contain complete forecast periods")
        year, month = map(int, self.start.split("-"))
        end = year * 12 + month - 1 + self.horizon - 1
        if end > 2099 * 12 + 11:
            raise ValueError("target period is outside the supported calendar")
        if self.kind == ForecastKind.HINDCAST and not (2004 * 12 <= year * 12 + month - 1 <= end <= 2024 * 12 + 11):
            raise ValueError("hindcast targets must be within 2004-01 through 2024-12")
        return self


class RegionFieldSpec(Timing):
    start: YearMonth
    region: RegionId = RegionId.KRAI
    mode: Literal[ForecastMode.SEASONAL] = ForecastMode.SEASONAL
    horizon: Literal[3] = 3
    season_len: Literal[3] = 3

    @model_validator(mode="before")
    @classmethod
    def strict_fixed_values(cls, value):
        if isinstance(value, dict):
            for key in ("horizon", "season_len"):
                if key in value and type(value[key]) is not int:
                    raise ValueError("regional periods must be integer values")
        return value

    @model_validator(mode="after")
    def regional_variables(self):
        year, month = map(int, self.start.split("-"))
        if year * 12 + month + 1 > 2099 * 12 + 11:
            raise ValueError("target period is outside the supported calendar")
        if set(self.variables) != {Variable.T2M, Variable.TP}:
            raise ValueError("regional field requires t2m and tp")
        return self


class ListQuery(Contract):
    limit: Annotated[int, Field(ge=1, le=100)] = 100

    @field_validator("limit", mode="before")
    @classmethod
    def integer_limit(cls, value):
        if isinstance(value, str):
            if not re.fullmatch(r"[1-9][0-9]*", value):
                raise ValueError("limit must be a positive decimal integer")
            return int(value)
        if type(value) is not int:
            raise ValueError("limit must be an integer")
        return value


class RegionQuery(Contract):
    region: RegionId = RegionId.KRAI


class ModeQuery(Contract):
    mode: ForecastMode = ForecastMode.SEASONAL


class ReportQuery(Contract):
    job: UUID


class EmptyQuery(Contract):
    pass


class QueueEventsQuery(Contract):
    since: Annotated[StrictInt, Field(ge=0)] = 0
    limit: Annotated[StrictInt, Field(ge=1, le=500)] = 200


class JobCancelBody(Contract):
    reason: Annotated[str, StringConstraints(strict=True, max_length=80)] | None = None


class DraftTiming(Timing):
    start_month: Month
    active: StrictBool = False

    @model_validator(mode="after")
    def inactive_only(self):
        if self.active:
            raise ValueError("subscription delivery is disabled")
        return self


class RegionFieldQuery(RegionQuery):
    start: YearMonth


def target_months(start, length):
    year, month = map(int, start.split("-"))
    first = year * 12 + month - 1
    return [f"{value // 12:04d}-{value % 12 + 1:02d}" for value in range(first, first + length)]
