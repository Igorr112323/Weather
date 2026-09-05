import logging
import logging.handlers
import os
import time
from html import escape
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from agrocast.core.contracts import (
    CONTRACT_VERSION, EmptyQuery, ForecastSpec,
    RegionFieldQuery, RegionFieldSpec, RegionId, RegionQuery, ReportQuery,
)
from agrocast.core.jsoncodec import strict_json
from agrocast.identity.credentials import IdentityError
from agrocast.identity.database import IdentitySettings
from agrocast.identity.service import IdentityService
from agrocast.serve.accounts import Actor, router as account_router
from agrocast.serve.browser_policy import ASSETS, BROWSER_HEADERS
from agrocast.serve.errors import APIError, ERROR_RESPONSES, error_response, invalid_request
from agrocast.serve.pilot import PILOT_REGION, PILOT_WARNING, historical_result, pilot_capabilities, pilot_points
from agrocast.serve.responses import (
    AcceptedJob, CapabilitiesResponse, DiagnosticResponse, GridResponse, LivenessResponse,
    RegionFieldResponse, RegionsResponse, SkillResponse, ValueResponse,
)
from agrocast.serve.security import AccessGuard, PUBLIC_GET_PATHS

STATIC = Path(__file__).resolve().parents[2] / "static"
WORLD = os.environ.get("AGROCAST_WORLD", str(STATIC.parent / "world"))
DATA_ROOT = os.environ.get("AGROCAST_DATA", str(STATIC.parent / "data"))
PrepareRequest = ForecastSpec
RegionRefreshRequest = RegionFieldSpec
JOBS = {}
NoQuery = Annotated[EmptyQuery, Query()]


def setup_logging():
    level = os.environ.get("AGROCAST_LOG_LEVEL", "INFO").upper()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s :: %(message)s")
    root = logging.getLogger()
    root.setLevel(level)
    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    root.addHandler(stream)
    try:
        directory = Path(DATA_ROOT) / "logs"
        directory.mkdir(parents=True, exist_ok=True)
        fileh = logging.handlers.RotatingFileHandler(directory / "app.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")
        fileh.setFormatter(fmt)
        root.addHandler(fileh)
    except OSError:
        pass
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).handlers = []
        logging.getLogger(name).propagate = True
    return logging.getLogger("agrocast")


log = setup_logging()
router = APIRouter(responses=ERROR_RESPONSES)


async def _request_log(request, call_next):
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        log.error("Unhandled HTTP failure: %s %s", request.method, request.url.path)
        response = error_response(APIError("internal_error", 500))
    log.info("%s %s -> %d (%.0f ms)", request.method, request.url.path, response.status_code, (time.perf_counter() - started) * 1000)
    return response


def disabled():
    raise APIError("pilot_operation_disabled", 403, PILOT_WARNING)


def validate_pilot_target(request):
    if request.region != RegionId.KRAI:
        raise APIError("pilot_region_disabled", 403)
    if isinstance(request, ForecastSpec):
        approved = {(point["lat"], point["lon"]): point["id"] for point in pilot_points()}
        point_id = approved.get((request.lat, request.lon))
        if point_id is None:
            raise APIError("pilot_point_disabled", 403)
        if request.point_id is not None and request.point_id != point_id:
            raise APIError("invalid_request", 422, "point_id does not match the coordinates")


@router.post("/api/prepare", response_model=AcceptedJob, status_code=202)
def prepare(req: PrepareRequest):
    validate_pilot_target(req)
    disabled()


@router.post("/api/region/refresh", response_model=AcceptedJob, status_code=202)
def region_refresh(req: RegionRefreshRequest):
    validate_pilot_target(req)
    disabled()


@router.get("/api/region/field", response_model=RegionFieldResponse)
def region_field(query: Annotated[RegionFieldQuery, Query()]):
    req = RegionFieldSpec(start=query.start, region=query.region)
    validate_pilot_target(req)
    disabled()


def artifact(path):
    try:
        value = strict_json(Path(path).read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("artifact must be an object")
        return value
    except (OSError, ValueError, RecursionError):
        raise APIError("artifact_unavailable", 503, "Исторический артефакт недоступен") from None


def region_grid(region: RegionId = RegionId.KRAI):
    from agrocast.serve.region import grid_path

    region = RegionId(region)
    grid = artifact(grid_path(WORLD, region))
    from agrocast.region.regions import region_name

    grid["region"] = region.value
    grid["region_name"] = region_name(region)
    if region == PILOT_REGION:
        approved = {point["id"]: (point["lat"], point["lon"]) for point in pilot_points()}
        grid["cells"] = [cell for cell in grid["cells"] if approved.get(cell["id"]) == (cell["lat"], cell["lon"])]
        grid["n_cells"] = len(grid["cells"])
    return historical_result({"ok": True, "grid": grid})


@router.get("/api/region/grid", response_model=GridResponse)
def grid_endpoint(query: Annotated[RegionQuery, Query()]):
    return region_grid(query.region)


def region_skill(region: RegionId = RegionId.KRAI):
    from agrocast.serve.region import skill_path

    return historical_result({"ok": True, "skill": artifact(skill_path(WORLD, RegionId(region)))})


@router.get("/api/region/skill", response_model=SkillResponse)
def skill_endpoint(query: Annotated[RegionQuery, Query()]):
    return region_skill(query.region)


@router.get("/api/region/regions", response_model=RegionsResponse)
def region_regions(query: NoQuery = EmptyQuery()):
    from agrocast.region.regions import region_summary

    rows = [row for row in region_summary(WORLD) if row["region"] == PILOT_REGION]
    return historical_result({"ok": True, "regions": rows})


@router.get("/api/health", response_model=DiagnosticResponse)
def health(query: NoQuery = EmptyQuery()):
    from agrocast.serve.pipeline import world_config

    out = {"ok": True, "world": str(WORLD)}
    try:
        store = world_config(WORLD).zarr_store()
        for name in ("daily_region", "sst", "fields_monthly", "strat_snow", "oisst_boxes", "regimes"):
            out[name] = str(store.last_time(name)) if store.exists(name) else None
    except Exception:
        raise APIError("diagnostics_unavailable", 503) from None
    return out


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def index():
    return (STATIC / "pilot.html").read_text(encoding="utf-8")


@router.get("/workspace", response_class=HTMLResponse, include_in_schema=False)
@router.get("/index.html", response_class=HTMLResponse, include_in_schema=False)
def workspace():
    return (STATIC / "index.html").read_text(encoding="utf-8").replace("{{PILOT_WARNING}}", escape(PILOT_WARNING))


@router.get("/report.html", response_class=HTMLResponse, include_in_schema=False)
def report_page(query: Annotated[ReportQuery, Query()], request: Request, actor: Actor):
    request.app.state.identity.get_resource("jobs", actor, str(query.job))
    return (STATIC / "report.html").read_text(encoding="utf-8").replace("{{PILOT_WARNING}}", escape(PILOT_WARNING))


@router.get("/api/value", response_model=ValueResponse)
def value_api(query: NoQuery = EmptyQuery()):
    return historical_result({"ok": True, "report": artifact(Path(WORLD) / "artifacts" / "value_report.json")})


@router.get("/value.html", response_class=HTMLResponse, include_in_schema=False)
def value_page():
    return (STATIC / "value.html").read_text(encoding="utf-8").replace("{{PILOT_WARNING}}", escape(PILOT_WARNING))


@router.get("/health/live", response_model=LivenessResponse)
def liveness(query: NoQuery = EmptyQuery()):
    return {"status": "alive", "stage": "closed_pilot", "forecast_enabled": False}


@router.get("/api/capabilities", response_model=CapabilitiesResponse)
def capabilities(query: NoQuery = EmptyQuery()):
    return pilot_capabilities()


@router.get("/pilot.js", include_in_schema=False)
def pilot_script():
    return FileResponse(STATIC / "pilot.js", media_type="application/javascript")


@router.get("/pilot.css", include_in_schema=False)
def pilot_styles():
    return FileResponse(STATIC / "pilot.css", media_type="text/css")


@router.get("/assets/{name:path}", include_in_schema=False)
def browser_asset(name: str):
    path = ASSETS.get("/assets/" + name)
    if path is None:
        raise HTTPException(status_code=404)
    return FileResponse(path)


@router.get("/api/contracts", response_model=dict, include_in_schema=False)
def contracts(request: Request, query: NoQuery = EmptyQuery()):
    return request.app.openapi()


def configured_identity():
    engine = None
    try:
        settings = IdentitySettings.from_environment()
        if settings is None:
            return None
        engine = settings.engine()
        return IdentityService(engine, settings.public_origin, settings.session_seconds)
    except Exception:
        if engine is not None:
            engine.dispose()
        log.error("Identity недоступна: проверьте конфигурацию, PostgreSQL и миграции")
        return None


def create_app(identity: IdentityService | None = None) -> FastAPI:
    application = FastAPI(title="AgroCast · закрытый пилот", version=CONTRACT_VERSION, docs_url=None, redoc_url=None, openapi_url=None)
    application.state.identity = identity if identity is not None else configured_identity()
    application.include_router(account_router)
    application.include_router(router)

    @application.exception_handler(Exception)
    async def unexpected_failure(request, error):
        log.error("Необработанная ошибка HTTP-контура")
        response = error_response(APIError("internal_error", 500))
        response.headers.update({**BROWSER_HEADERS, "Vary": "Cookie"})
        return response

    @application.exception_handler(IdentityError)
    async def identity_failure(request, error):
        return error_response(error)

    @application.exception_handler(SQLAlchemyError)
    async def database_failure(request, error):
        log.error("Операция identity завершилась ошибкой базы данных")
        return error_response(APIError("identity_unavailable", 503))

    @application.exception_handler(RequestValidationError)
    async def bad_request(request, error):
        return invalid_request(error)

    @application.exception_handler(ResponseValidationError)
    async def bad_response(request, error):
        log.error("HTTP response contract failed: %s", request.url.path)
        return error_response(APIError("invalid_response", 500))

    @application.exception_handler(StarletteHTTPException)
    async def http_failure(request, error):
        code = {404: "resource_not_found", 405: "method_not_allowed"}.get(error.status_code, "http_error")
        response = error_response(APIError(code, error.status_code))
        response.headers.update(error.headers or {})
        return response

    def openapi():
        if application.openapi_schema is None:
            schema = get_openapi(title=application.title, version=CONTRACT_VERSION, routes=application.routes)
            schema.setdefault("components", {})["securitySchemes"] = {
                "SessionCookie": {"type": "apiKey", "in": "cookie", "name": "__Host-agrocast_session"},
                "CSRF": {"type": "apiKey", "in": "header", "name": "X-CSRF-Token"},
            }
            for path, methods in schema["paths"].items():
                for method, operation in methods.items():
                    if method not in {"get", "head", "post", "put", "patch", "delete"}:
                        continue
                    if path not in PUBLIC_GET_PATHS and path != "/api/auth/login":
                        operation["security"] = [{"SessionCookie": [], **({"CSRF": []} if method not in {"get", "head"} else {})}]
                    if method not in {"get", "head"}:
                        operation.setdefault("parameters", []).append({"name": "Origin", "in": "header", "required": True, "schema": {"type": "string", "description": "Exact configured HTTPS origin"}})
            for path in ("/api/prepare", "/api/region/refresh"):
                schema["paths"][path]["post"]["x-pilot-admission"] = "disabled"
            schema["x-pilot-computation-enabled"] = False
            application.openapi_schema = schema
        return application.openapi_schema

    application.openapi = openapi
    application.middleware("http")(_request_log)
    application.add_middleware(AccessGuard, identity=application.state.identity)
    return application


app = create_app()
