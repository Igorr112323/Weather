import re
import secrets

from sqlalchemy.exc import SQLAlchemyError
from starlette._utils import get_route_path
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers, MutableHeaders, QueryParams
from starlette.responses import RedirectResponse

from agrocast.identity.credentials import IdentityError
from agrocast.identity.service import ADMIN_ROLES, ALL_ROLES, WRITE_ROLES
from agrocast.serve.pilot import PILOT_REGION, PILOT_VERSION, PILOT_WARNING
from agrocast.serve.browser_policy import ASSETS, browser_headers
from agrocast.core.jsoncodec import strict_json
from agrocast.serve.errors import APIError, error_response

SESSION_COOKIE = "__Host-agrocast_session"
PUBLIC_GET_PATHS = frozenset({"/health/live", "/api/capabilities", "/login", "/login.js", "/pilot.css", *ASSETS})
PILOT_READ_PATHS = frozenset({
    "/", "/pilot.js", "/value.html", "/api/value", "/api/region/grid",
    "/api/region/regions", "/api/region/skill", "/api/auth/me",
    "/workspace", "/index.html", "/report.html", "/api/contracts",
})
BODY_LIMIT = 16384
RESOURCE_ID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"


def session_token(headers):
    values = []
    for line in headers.getlist("cookie"):
        for part in line.split(";"):
            key, separator, value = part.strip().partition("=")
            if key == SESSION_COOKIE and separator:
                values.append(value)
    return values[0] if len(values) == 1 else ""


def allowed_roles(method, path):
    if method in {"GET", "HEAD"}:
        if path in PILOT_READ_PATHS:
            return ALL_ROLES
        if path in {"/api/health", "/api/admin/users", "/api/admin/events"}:
            return ADMIN_ROLES
        if re.fullmatch(rf"/api/(fields|crops|subscriptions|jobs)(/{RESOURCE_ID})?", path) or re.fullmatch(rf"/api/job/{RESOURCE_ID}", path):
            return ALL_ROLES
    if method == "POST" and path in {"/api/auth/logout", "/api/auth/password"}:
        return ALL_ROLES
    if method == "POST" and path == "/api/admin/users":
        return ADMIN_ROLES
    if method == "PATCH" and re.fullmatch(rf"/api/admin/users/{RESOURCE_ID}", path):
        return ADMIN_ROLES
    if method == "POST" and path in {"/api/fields", "/api/subscriptions", "/api/prepare", "/api/region/refresh"}:
        return WRITE_ROLES
    if method in {"PUT", "DELETE"} and re.fullmatch(rf"/api/(fields|subscriptions)/{RESOURCE_ID}", path):
        return WRITE_ROLES
    if (method == "POST" and path == "/api/crops") or (method in {"PUT", "DELETE"} and re.fullmatch(rf"/api/crops/{RESOURCE_ID}", path)):
        return ADMIN_ROLES
    if method == "DELETE" and re.fullmatch(rf"/api/jobs/{RESOURCE_ID}", path):
        return WRITE_ROLES
    return frozenset()


def identity_error(error):
    return error_response(error)


class AccessGuard:
    def __init__(self, app, identity=None, retired=False):
        self.app = app
        self.identity = identity
        self.retired = retired

    async def __call__(self, scope, receive, send):
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def secure_send(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                browser_headers(headers)
                headers["X-AgroCast-Stage"] = PILOT_VERSION
            await send(message)

        if self.retired:
            await error_response(APIError("legacy_api_disabled", 410, "Используйте agrocast.serve.product:app"))(scope, receive, secure_send)
            return
        method = scope["method"]
        path = get_route_path(scope).rstrip("/") or "/"
        headers = Headers(scope=scope)
        if method in {"GET", "HEAD"} and path in PUBLIC_GET_PATHS:
            await self.app(scope, receive, secure_send)
            return
        if self.identity is None:
            await identity_error(IdentityError("identity_unavailable", 503))(scope, receive, secure_send)
            return
        try:
            principal = None
            if not (method == "POST" and path == "/api/auth/login"):
                try:
                    principal = await run_in_threadpool(self.identity.authenticate, session_token(headers))
                except IdentityError as error:
                    if error.status == 401 and method == "GET" and path in {"/", "/value.html", "/workspace", "/index.html", "/report.html"}:
                        prefix = scope.get("root_path", "")
                        await RedirectResponse(prefix + "/login", status_code=303)(scope, receive, secure_send)
                        return
                    raise
                roles = allowed_roles(method, path)
                if not roles:
                    await error_response(APIError("pilot_operation_disabled", 403, PILOT_WARNING))(scope, receive, secure_send)
                    return
                if principal.role not in roles:
                    raise IdentityError("role_forbidden", 403)
                scope.setdefault("state", {})["principal"] = principal
            if path.startswith("/api/region/"):
                regions = QueryParams(scope.get("query_string", b"")).getlist("region")
                if any(region != PILOT_REGION for region in regions):
                    raise IdentityError("pilot_region_disabled", 403)
            query_pairs = QueryParams(scope.get("query_string", b"")).multi_items()
            if len({key for key, _ in query_pairs}) != len(query_pairs):
                raise APIError("invalid_request", 422, "Duplicate query parameters are not allowed")
            if method not in {"GET", "HEAD"}:
                origins = headers.getlist("origin")
                if origins != [self.identity.public_origin]:
                    raise IdentityError("origin_forbidden", 403)
                if principal is not None:
                    csrf = headers.getlist("x-csrf-token")
                    if len(csrf) != 1 or len(csrf[0]) != 43 or not csrf[0].isascii() or not secrets.compare_digest(csrf[0], principal.csrf_token):
                        raise IdentityError("csrf_failed", 403)
                length = headers.getlist("content-length")
                if len(length) > 1 or (length and (len(length[0]) > 8 or not length[0].isdigit() or int(length[0]) > BODY_LIMIT)):
                    raise IdentityError("request_too_large", 413)
                if method in {"POST", "PUT", "PATCH"} and path != "/api/auth/logout":
                    if headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
                        raise IdentityError("json_required", 415)
                data = bytearray()
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    data.extend(message.get("body", b""))
                    if len(data) > BODY_LIMIT:
                        raise IdentityError("request_too_large", 413)
                    if not message.get("more_body", False):
                        break
                if data and method in {"POST", "PUT", "PATCH"}:
                    try:
                        strict_json(data.decode("utf-8"))
                    except (ValueError, RecursionError):
                        raise APIError("invalid_request", 422, detail=[{"loc": ["body"], "type": "json_invalid", "msg": "JSON must use UTF-8, unique keys and finite numbers"}]) from None
                delivered = False

                async def replay():
                    nonlocal delivered
                    if not delivered:
                        delivered = True
                        return {"type": "http.request", "body": bytes(data), "more_body": False}
                    return await receive()

                await self.app(scope, replay, secure_send)
                return
            await self.app(scope, receive, secure_send)
        except IdentityError as error:
            await identity_error(error)(scope, receive, secure_send)
        except SQLAlchemyError:
            await identity_error(IdentityError("identity_unavailable", 503))(scope, receive, secure_send)
