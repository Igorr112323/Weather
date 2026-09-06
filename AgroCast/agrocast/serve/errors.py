from typing import Annotated

from pydantic import Field, StrictInt, StrictStr
from starlette.responses import JSONResponse

from agrocast.core.contracts import Contract
from agrocast.identity.credentials import IdentityError


class ValidationIssue(Contract):
    loc: list[StrictStr | StrictInt]
    type: str
    msg: str


class ErrorResponse(Contract):
    code: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]
    error: str
    detail: list[ValidationIssue] | None = None


class APIError(IdentityError):
    def __init__(self, code, status, message=None, detail=None, retry_after=None):
        super().__init__(code, status, retry_after)
        self.message = message or code
        self.detail = detail


def error_response(error):
    payload = ErrorResponse(
        code=error.code, error=getattr(error, "message", error.code),
        detail=getattr(error, "detail", None),
    )
    headers = {"Retry-After": str(error.retry_after)} if error.retry_after is not None else {}
    return JSONResponse(payload.model_dump(mode="json", exclude_none=True), status_code=error.status, headers=headers)


def invalid_request(error):
    issues = [{"loc": list(row["loc"]), "type": row["type"], "msg": row["msg"]} for row in error.errors()]
    return error_response(APIError("invalid_request", 422, detail=issues))


ERROR_RESPONSES = {
    status: {"model": ErrorResponse, "description": description}
    for status, description in {
        401: "Authentication required", 403: "Role, scope, CSRF or pilot policy forbids the operation",
        404: "Resource absent or not visible to this owner", 409: "Resource version or state conflict",
        413: "Request body too large", 415: "JSON required", 422: "Invalid request",
        429: "Rate limited", 500: "Internal error or invalid response", 503: "Required service or artifact unavailable",
    }.items()
}
