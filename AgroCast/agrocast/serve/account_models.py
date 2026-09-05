from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, StrictBool, StrictFloat, field_validator, model_validator

from agrocast.core.contracts import DraftTiming, VarietySelection
from agrocast.identity.credentials import Role
from agrocast.serve.pilot import pilot_points

Username = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.@-]+$")]
Name = Annotated[str, Field(min_length=1, max_length=120)]
NewPassword = Annotated[SecretStr, Field(min_length=15, max_length=128)]


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, str_strip_whitespace=True)

    @field_validator("*", mode="before")
    @classmethod
    def no_nul(cls, value):
        if isinstance(value, str) and ("\x00" in value or any(0xD800 <= ord(char) <= 0xDFFF for char in value)):
            raise ValueError("NUL and surrogate characters are not supported in text values")
        return value


class LoginBody(Body):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    username: Username
    password: Annotated[SecretStr, Field(min_length=1, max_length=128)]


class PasswordBody(Body):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    current_password: Annotated[SecretStr, Field(min_length=1, max_length=128)]
    new_password: NewPassword


class UserBody(Body):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    username: Username
    password: NewPassword
    role: Role


class UserUpdateBody(Body):
    role: Role | None = None
    active: StrictBool | None = None

    @model_validator(mode="after")
    def require_change(self):
        if self.role is None and self.active is None:
            raise ValueError("role or active is required")
        return self


class FieldBody(Body):
    name: Name
    point_id: str
    area_ha: Annotated[StrictFloat, Field(gt=0, le=1000000)]

    @field_validator("point_id")
    @classmethod
    def supported_point(cls, value):
        if value not in {point["id"] for point in pilot_points()}:
            raise ValueError("point is outside the closed pilot")
        return value


class SubscriptionBody(Body, DraftTiming, VarietySelection):
    name: Name
    field_id: UUID


class CropBody(Body):
    name: Name
    breeder: Annotated[str, Field(max_length=120)] = ""
    notes: Annotated[str, Field(max_length=2000)] = ""
    fao: Annotated[int, Field(ge=50, le=650, strict=True)] | None = None
    gdd: Annotated[StrictFloat, Field(ge=1500, le=3500)] | None = None
    vp_days: Annotated[int, Field(ge=60, le=200, strict=True)] | None = None
    yield_t_ha: Annotated[StrictFloat, Field(gt=0, le=30)] | None = None
    frost_tol_c: Annotated[StrictFloat, Field(ge=-8, le=0)] = -2
    frost_fatal_c: Annotated[StrictFloat, Field(ge=-8, le=0)] = -3

    @model_validator(mode="after")
    def frost_limits(self):
        if self.frost_fatal_c > self.frost_tol_c:
            raise ValueError("fatal frost must not be warmer than tolerable frost")
        return self
