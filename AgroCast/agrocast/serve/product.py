import logging
import time
from dataclasses import replace
from html import escape
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from agrocast.core.contracts import (
    CONTRACT_VERSION, EmptyQuery, ForecastSpec,
    RegionFieldQuery, RegionFieldSpec, RegionId, RegionQuery, ReportQuery,
)
from agrocast.core.errors import IssueFreshnessError
from agrocast.core.jsoncodec import strict_json
from agrocast.identity.credentials import IdentityError
from agrocast.core.settings import RuntimeSettings
from agrocast.queue.admission import admit_point, admit_region
from agrocast.serve.runtime import runtime_lifespan
from agrocast.identity.service import IdentityService
from agrocast.serve.accounts import Actor, router as account_router
from agrocast.serve.browser_policy import ASSETS, BROWSER_HEADERS
from agrocast.serve.errors import APIError, ERROR_RESPONSES, error_response, invalid_request
from agrocast.serve.pilot import PILOT_REGION, PILOT_WARNING, historical_result, pilot_capabilities, pilot_points
from agrocast.serve.queue_api import router as queue_router
from agrocast.serve.responses import (
    AcceptedJob, CapabilitiesResponse, DiagnosticResponse, GridResponse, LivenessResponse, LocalForecastResponse,
    LocalInputsResponse, ReadinessResponse, RegionFieldResponse, RegionsResponse, SkillResponse, ValueResponse,
    accepted_job_response,
)
from agrocast.serve.security import AccessGuard, PUBLIC_GET_PATHS

from agrocast.core.settings import bundle_static_dir

STATIC = bundle_static_dir()
PrepareRequest = ForecastSpec
RegionRefreshRequest = RegionFieldSpec
NoQuery = Annotated[EmptyQuery, Query()]


log = logging.getLogger("agrocast")
router = APIRouter(responses=ERROR_RESPONSES)


async def _request_log(request, call_next):
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        log.error("Unhandled HTTP failure: %s %s", request.method, request.url.path)
        response = error_response(APIError("internal_error", 500))
    request.app.state.logger.info("%s %s -> %d (%.0f ms)", request.method, request.url.path, response.status_code, (time.perf_counter() - started) * 1000)
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
def prepare(req: PrepareRequest, request: Request, actor: Actor):
    validate_pilot_target(req)
    if not request.app.state.settings.queue_intake:
        disabled()
    result = admit_point(request.app.state.queue, request.app.state.identity, request.app.state.settings, actor, req)
    response = accepted_job_response(result["job_id"])
    response.headers["X-Deduplicated"] = "true" if result["deduplicated"] else "false"
    response.headers["X-Cached"] = "true" if result.get("cached") else "false"
    return response


@router.post("/api/region/refresh", response_model=AcceptedJob, status_code=202)
def region_refresh(req: RegionRefreshRequest, request: Request, actor: Actor):
    validate_pilot_target(req)
    if not request.app.state.settings.queue_intake:
        disabled()
    result = admit_region(request.app.state.queue, request.app.state.settings, actor, req)
    response = accepted_job_response(result["job_id"])
    response.headers["X-Deduplicated"] = "true" if result["deduplicated"] else "false"
    response.headers["X-Cached"] = "true" if result.get("cached") else "false"
    return response


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


def region_grid(region: RegionId = RegionId.KRAI, world_dir=None):
    from agrocast.serve.region import grid_path

    region = RegionId(region)
    grid = artifact(grid_path(world_dir or RuntimeSettings.from_environment().world_dir, region))
    from agrocast.region.regions import region_name

    grid["region"] = region.value
    grid["region_name"] = region_name(region)
    if region == PILOT_REGION:
        approved = {point["id"]: (point["lat"], point["lon"]) for point in pilot_points()}
        grid["cells"] = [cell for cell in grid["cells"] if approved.get(cell["id"]) == (cell["lat"], cell["lon"])]
        grid["n_cells"] = len(grid["cells"])
    return historical_result({"ok": True, "grid": grid})


@router.get("/api/region/grid", response_model=GridResponse)
def grid_endpoint(query: Annotated[RegionQuery, Query()], request: Request):
    return region_grid(query.region, request.app.state.settings.world_dir)


def region_skill(region: RegionId = RegionId.KRAI, world_dir=None):
    from agrocast.serve.region import skill_path

    return historical_result({"ok": True, "skill": artifact(skill_path(world_dir or RuntimeSettings.from_environment().world_dir, RegionId(region)))})


@router.get("/api/region/skill", response_model=SkillResponse)
def skill_endpoint(query: Annotated[RegionQuery, Query()], request: Request):
    return region_skill(query.region, request.app.state.settings.world_dir)


def region_regions(world_dir=None):
    from agrocast.region.regions import region_summary

    rows = [row for row in region_summary(world_dir or RuntimeSettings.from_environment().world_dir) if row["region"] == PILOT_REGION]
    return historical_result({"ok": True, "regions": rows})


@router.get("/api/region/regions", response_model=RegionsResponse)
def regions_endpoint(request: Request, query: NoQuery = EmptyQuery()):
    return region_regions(request.app.state.settings.world_dir)


@router.get("/api/health", response_model=DiagnosticResponse)
def health(request: Request, query: NoQuery = EmptyQuery()):
    out = {"ok": True, "world": str(request.app.state.settings.world_dir)}
    try:
        store = request.app.state.config.zarr_store()
        for name in ("daily_region", "sst", "fields_monthly", "strat_snow", "oisst_boxes", "regimes"):
            out[name] = str(store.last_time(name)) if store.exists(name) else None
    except Exception:
        raise APIError("diagnostics_unavailable", 503) from None
    return out


@router.get("/api/local/inputs", response_model=LocalInputsResponse)
def local_inputs(request: Request, query: NoQuery = EmptyQuery()):
    settings = request.app.state.settings
    if not settings.desktop_mode:
        raise APIError("desktop_only", 403, "Локальные входные данные доступны только в десктоп-версии")
    from agrocast.serve.local import collect_inputs

    return collect_inputs(settings)


@router.get("/api/local/autonomy")
def local_autonomy(request: Request, query: NoQuery = EmptyQuery()):
    settings = request.app.state.settings
    if not settings.desktop_mode:
        raise APIError("desktop_only", 403)
    from pathlib import Path as _P

    world = _P(settings.world_dir)
    ready = world / "ready.json"
    config = world / "config.json"
    zarr_dir = world / "zarr"
    zarr_ok = zarr_dir.is_dir() and any(zarr_dir.iterdir()) if zarr_dir.exists() else False
    all_ok = ready.exists() and config.exists() and zarr_ok
    return {
        "autonomous": bool(all_ok),
        "checks": {
            "world_ready": {"ok": ready.exists()},
            "config": {"ok": config.exists()},
            "zarr_data": {"ok": zarr_ok},
        },
        "internet_required": False,
    }


@router.post("/api/local/forecast", response_model=LocalForecastResponse)
def local_forecast(request: Request, spec: ForecastSpec):
    settings = request.app.state.settings
    if not settings.desktop_mode:
        raise APIError("desktop_only", 403, "Локальный расчёт доступен только в десктоп-версии")
    config = request.app.state.config
    region = config.region
    if not (region.lat_min <= spec.lat <= region.lat_max and region.lon_min <= spec.lon <= region.lon_max):
        raise APIError("point_outside_region", 422, "Точка вне покрытия локального набора данных")
    from agrocast.serve.local import run_forecast

    principal = getattr(request.state, "principal", None)
    try:
        return run_forecast(settings, spec, principal)
    except IssueFreshnessError:
        raise APIError("issue_inputs_mismatch", 422, "Запрошенный месяц новее последних полных входов; прогноз не публикуется") from None
    except (ValueError, RuntimeError) as exc:
        log.error("Локальный расчёт не завершился: %s: %s", type(exc).__name__, exc)
        raise APIError("compute_failed", 500, str(exc)) from None
    except Exception as exc:
        log.error("Необработанная ошибка локального расчёта: %s: %s", type(exc).__name__, exc)
        raise APIError("compute_failed", 500, "Локальный расчёт не завершился; входные данные и кэш сохранены") from None


@router.post("/api/local/hindcast")
def local_hindcast(request: Request, spec: ForecastSpec):
    settings = request.app.state.settings
    if not settings.desktop_mode:
        raise APIError("desktop_only", 403, "Локальный расчёт доступен только в десктоп-версии")
    config = request.app.state.config
    region = config.region
    if not (region.lat_min <= spec.lat <= region.lat_max and region.lon_min <= spec.lon <= region.lon_max):
        raise APIError("point_outside_region", 422, "Точка вне покрытия локального набора данных")
    from agrocast.serve.local import use_active_bundle
    from agrocast.serve.pipeline import ensure_point, point_config, run_hindcast
    import pandas as pd

    settings = use_active_bundle(settings)[0]
    try:
        cfg, _ = point_config(
            settings.world_dir, settings.state_dir,
            float(spec.lat), float(spec.lon),
            settings.compute_config().to_dict(),
        )
        ensure_point(cfg, settings.world_dir, lambda message: None)
        # Extract year from spec.start
        start_str = str(spec.start)
        start_period = pd.Period(start_str, "M")
        year = start_period.year
        # Build start of the year for hindcast
        year_start = f"{year}-01"
        result = run_hindcast(
            cfg, float(spec.lat), float(spec.lon),
            start=year_start, mode="seasonal", horizon=12,
            log=lambda message: None,
        )
        return result
    except (ValueError, RuntimeError) as exc:
        log.error("Hindcast не завершился: %s: %s", type(exc).__name__, exc)
        raise APIError("compute_failed", 500, str(exc)) from None
    except Exception as exc:
        log.error("Необработанная ошибка hindcast: %s: %s", type(exc).__name__, exc)
        raise APIError("compute_failed", 500, "Проверка на истории не завершилась") from None


@router.get("/desktop.html", response_class=HTMLResponse, include_in_schema=False)
def desktop_page(request: Request):
    if not request.app.state.settings.desktop_mode:
        raise APIError("desktop_only", 403)
    return (STATIC / "desktop.html").read_text(encoding="utf-8")


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def index(request: Request):
    if request.app.state.settings.desktop_mode:
        return (STATIC / "desktop.html").read_text(encoding="utf-8")
    return (STATIC / "pilot.html").read_text(encoding="utf-8")


@router.get("/workspace", response_class=HTMLResponse, include_in_schema=False)
@router.get("/index.html", response_class=HTMLResponse, include_in_schema=False)
def workspace():
    return (STATIC / "index.html").read_text(encoding="utf-8").replace("{{PILOT_WARNING}}", escape(PILOT_WARNING))


@router.get("/report.html", response_class=HTMLResponse, include_in_schema=False)
def report_page(query: Annotated[ReportQuery, Query()], request: Request, actor: Actor):
    request.app.state.identity.get_resource("jobs", actor, str(query.job))
    return (STATIC / "report.html").read_text(encoding="utf-8").replace("{{PILOT_WARNING}}", escape(PILOT_WARNING))


def value_api(world_dir=None):
    return historical_result({"ok": True, "report": artifact(Path(world_dir or RuntimeSettings.from_environment().world_dir) / "artifacts" / "value_report.json")})


@router.get("/api/value", response_model=ValueResponse)
def value_endpoint(request: Request, query: NoQuery = EmptyQuery()):
    return value_api(request.app.state.settings.world_dir)


@router.get("/value.html", response_class=HTMLResponse, include_in_schema=False)
def value_page():
    return (STATIC / "value.html").read_text(encoding="utf-8").replace("{{PILOT_WARNING}}", escape(PILOT_WARNING))


@router.get("/health/live", response_model=LivenessResponse)
def liveness(query: NoQuery = EmptyQuery()):
    return {"status": "alive", "stage": "closed_pilot", "forecast_enabled": False}


@router.get("/health/ready", response_model=ReadinessResponse)
def readiness_endpoint(request: Request, query: NoQuery = EmptyQuery()):
    from agrocast.serve import readiness as readiness_service

    settings = request.app.state.settings
    engine = getattr(getattr(request.app.state, "identity", None), "engine", None)
    result = readiness_service.evaluate(settings, request.app.state.config, engine=engine)
    return JSONResponse(result, status_code=200 if result["status"] == "ready" else 503)


@router.get("/api/capabilities", response_model=CapabilitiesResponse)
def capabilities(request: Request, query: NoQuery = EmptyQuery()):
    out = pilot_capabilities()
    intake = bool(request.app.state.settings.queue_intake)
    out["operations"]["durable_queue"] = True
    out["operations"]["local_mode"] = bool(request.app.state.settings.desktop_mode)
    out["operations"]["queue_intake"] = intake
    out["operations"]["region_refresh"] = intake
    out["operations"]["hindcast"] = intake
    return out


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


def create_app(identity: IdentityService | None = None, settings: RuntimeSettings | None = None) -> FastAPI:
    if settings is None:
        settings = RuntimeSettings.from_environment()
        if identity is not None:
            settings = replace(settings, public_origin=identity.public_origin, session_seconds=identity.session_seconds)
    application = FastAPI(title="AgroCast · закрытый пилот", version=CONTRACT_VERSION, docs_url=None, redoc_url=None, openapi_url=None, lifespan=runtime_lifespan(settings, identity))
    application.state.settings = settings
    application.state.identity = identity
    application.state.logger = log
    application.state.started = False
    application.include_router(account_router)
    application.include_router(queue_router)
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
            admission = "enabled" if settings.queue_intake else "disabled"
            for path in ("/api/prepare", "/api/region/refresh"):
                schema["paths"][path]["post"]["x-pilot-admission"] = admission
            schema["x-pilot-computation-enabled"] = False
            schema["x-durable-queue"] = {"admission": admission, "states": ["queued", "running", "succeeded", "failed", "cancelled"]}
            application.openapi_schema = schema
        return application.openapi_schema

    application.openapi = openapi
    application.middleware("http")(_request_log)
    application.add_middleware(AccessGuard)
    return application
