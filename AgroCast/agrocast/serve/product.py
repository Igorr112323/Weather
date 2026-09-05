import json
import logging
import logging.handlers
import math
import os
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agrocast.geo.russia import inside_russia, nearest_inside_distance_km

STATIC = Path(__file__).resolve().parent.parent.parent / "static"
WORLD = os.environ.get("AGROCAST_WORLD", str(Path(__file__).resolve().parent.parent.parent / "world"))
DATA_ROOT = os.environ.get("AGROCAST_DATA", str(Path(__file__).resolve().parent.parent.parent / "data"))


def setup_logging():
    level = os.environ.get("AGROCAST_LOG_LEVEL", "INFO").upper()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s :: %(message)s")
    root = logging.getLogger()
    root.setLevel(level)
    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    root.addHandler(stream)
    try:
        log_dir = Path(DATA_ROOT) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        fileh = logging.handlers.RotatingFileHandler(
            log_dir / "app.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
        )
        fileh.setFormatter(fmt)
        root.addHandler(fileh)
    except OSError:
        pass
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True
    return logging.getLogger("agrocast")


log = setup_logging()
log.info("AgroCast стартовал: world=%s data=%s", WORLD, DATA_ROOT)

app = FastAPI(title="AgroCast Россия")

JOBS = {}
_CROPS = None


def _cropdb():
    global _CROPS
    if _CROPS is None:
        from agrocast.crops.db import CropDB

        _CROPS = CropDB(
            Path(DATA_ROOT) / "crops.db",
            str(Path(WORLD) / "artifacts" / "crop_seed.json"),
        )
    return _CROPS


@app.middleware("http")
async def _request_log(request, call_next):
    t0 = time.perf_counter()
    try:
        resp = await call_next(request)
    except Exception as exc:
        log.exception("unhandled %s %s: %s", request.method, request.url.path, exc)
        return JSONResponse({"error": "внутренняя ошибка"}, status_code=500)
    dt_ms = (time.perf_counter() - t0) * 1000
    log.info("%s %s -> %d (%.0f ms)", request.method, request.url.path, resp.status_code, dt_ms)
    return resp


class PrepareRequest(BaseModel):
    lat: float
    lon: float
    start: str = ""
    horizon: int = 3
    mode: str = "seasonal"
    season_len: int = 3
    kind: str = "forecast"
    year: int = 2020
    variety: str = ""


class SubscribeRequest(BaseModel):
    name: str
    lat: float
    lon: float
    start_month: int
    horizon: int = 3
    mode: str = "seasonal"


@app.get("/api/years")
def years():
    from agrocast.serve.pipeline import HINDCAST_YEARS

    return {"years": HINDCAST_YEARS}


@app.get("/api/inside")
def inside(lat: float, lon: float):
    ok = bool(inside_russia(lat, lon))
    dist = 0.0 if ok else round(nearest_inside_distance_km(lat, lon), 0)
    return {"inside": ok, "nearest_russia_km": dist}


@app.post("/api/prepare")
def prepare(req: PrepareRequest):
    if not inside_russia(req.lat, req.lon):
        log.warning("prepare отклонён: (%.2f, %.2f) вне России", req.lat, req.lon)
        return JSONResponse({"error": "Продукт поддерживает только территорию России"}, status_code=400)
    job_id = uuid.uuid4().hex[:10]
    from agrocast.serve.pipeline import Job, start_job

    job = Job(job_id, req.model_dump())
    JOBS[job_id] = job
    log.info(
        "job %s: mode=%s (%.2f, %.2f) kind=%s horizon=%d year=%s start=%s",
        job_id, req.mode, req.lat, req.lon, req.kind, req.horizon, req.year, req.start,
    )
    start_job(job, WORLD, DATA_ROOT)
    return {"job": job_id}


def _clean(o):
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, float) and not math.isfinite(o):
        return None
    return o


@app.get("/api/job/{job_id}")
def job_state(job_id: str):
    job = JOBS.get(job_id)
    if job is None:
        return JSONResponse({"error": "задача не найдена"}, status_code=404)
    out = {"status": job.status, "log": job.log}
    if job.status == "error":
        log.error("job %s завершился ошибкой: %s", job_id, job.error)
        out["error"] = job.error
    if job.status == "done":
        out["report"] = _clean(job.result)
    return out


@app.post("/api/subscribe")
def subscribe(req: SubscribeRequest):
    if not inside_russia(req.lat, req.lon):
        log.warning("subscribe отклонён: (%.2f, %.2f) вне России", req.lat, req.lon)
        return JSONResponse({"error": "Продукт поддерживает только территорию России"}, status_code=400)
    from agrocast.ingest.registry import Registry
    from agrocast.serve.pipeline import world_config

    cfg = world_config(WORLD)
    reg = Registry(cfg.registry_path)
    reg.add_subscription(req.name, req.lat, req.lon, req.horizon, ("t2m", "tp"), req.mode)
    log.info("подписка: name=%s (%.2f, %.2f) mode=%s horizon=%d", req.name, req.lat, req.lon, req.mode, req.horizon)
    return {"ok": True, "name": req.name}


@app.get("/api/crops")
def crops_list():
    return {"ok": True, "crops": _cropdb().all()}


@app.post("/api/crops")
def crops_add(d: dict):
    try:
        row = _cropdb().upsert(d)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    log.info("справочник: сорт «%s» сохранён", row["name"])
    return {"ok": True, "crop": row}


@app.delete("/api/crops/{name}")
def crops_del(name: str):
    ok = _cropdb().remove(name)
    if not ok:
        return JSONResponse({"error": "сорт не найден"}, status_code=404)
    log.info("справочник: сорт «%s» удалён", name)
    return {"ok": True}


class RegionRefreshRequest(BaseModel):
    start: str = "2026-10"
    region: str = "krai"


def _unknown_region(region):
    from agrocast.serve import region as region_mod

    if region_mod.known_region(region):
        return None
    return JSONResponse({"error": f"неизвестный регион: {region}"}, status_code=400)


@app.get("/api/region/grid")
def region_grid(region: str = "krai"):
    from agrocast.serve import region as region_mod

    bad = _unknown_region(region)
    if bad is not None:
        return bad
    return region_mod.grid_payload(WORLD, region)


@app.get("/api/region/skill")
def region_skill(region: str = "krai"):
    from agrocast.serve import region as region_mod

    bad = _unknown_region(region)
    if bad is not None:
        return bad
    return region_mod.skill_payload(WORLD, region)


@app.get("/api/region/field")
def region_field(region: str = "krai"):
    from agrocast.serve import region as region_mod

    bad = _unknown_region(region)
    if bad is not None:
        return bad
    return _clean(region_mod.field_payload(DATA_ROOT, region))


@app.get("/api/region/regions")
def region_regions():
    from agrocast.region.regions import region_summary

    return {"ok": True, "regions": region_summary(WORLD)}


def _start_region_job(job, world_dir, data_root, start, region):
    def run():
        try:
            from agrocast.serve import region as region_mod

            fp = region_mod.build_field(start, world_dir, data_root, region=region, workers=2, log=job.add)
            job.result = {"field": str(fp), "region": region}
            job.status = "done"
            job.add("готово")
        except Exception as exc:
            job.status = "error"
            job.error = f"{type(exc).__name__}: {exc}"
            job.add("ошибка: " + job.error)

    threading.Thread(target=run, daemon=True).start()


@app.post("/api/region/refresh")
def region_refresh(req: RegionRefreshRequest):
    from agrocast.serve import region as region_mod
    from agrocast.serve.pipeline import Job

    bad = _unknown_region(req.region)
    if bad is not None:
        return bad
    p = region_mod.field_path(DATA_ROOT, req.region)
    if p.exists() and time.time() - p.stat().st_mtime < 600:
        return {"ok": True, "fresh": True, "job": None, "region": req.region}
    job_id = uuid.uuid4().hex[:10]
    job = Job(job_id, {"kind": "region_field", "start": req.start, "region": req.region})
    JOBS[job_id] = job
    log.info("region job %s: поле региона %s, старт %s", job_id, req.region, req.start)
    _start_region_job(job, WORLD, DATA_ROOT, req.start, req.region)
    return {"ok": True, "fresh": False, "job": job_id, "region": req.region}


@app.get("/api/ledger")
def ledger(mode: str = "seasonal"):
    """Публичный счёт навыка: реестр доверия + живые выпуски."""
    from agrocast.serve.pipeline import world_config
    from agrocast.skill.ledger import live_summary, load_ledger

    cfg = world_config(WORLD)
    _, s = load_ledger(cfg, mode if mode in ("monthly", "seasonal") else "seasonal")
    lv = live_summary(cfg)
    if s is None:
        return {"ok": True, "ledger": None, "live": lv}
    out = dict(s)
    if lv:
        out["live"] = lv
    return {"ok": True, "ledger": out, "live": lv}


@app.get("/api/health")
def health():
    from agrocast.serve.pipeline import world_config

    cfg = world_config(WORLD)
    out = {"ok": True, "world": str(WORLD)}
    try:
        st = cfg.zarr_store()
        for name in ["daily_region", "sst", "fields_monthly", "strat_snow", "oisst_boxes", "regimes"]:
            out[name] = str(st.last_time(name)) if st.exists(name) else None
    except Exception as exc:
        out["ok"] = False
        out["error"] = str(exc)[:200]
    return out


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC / "index.html").read_text(encoding="utf-8")


@app.get("/report.html", response_class=HTMLResponse)
def report_page():
    return (STATIC / "report.html").read_text(encoding="utf-8")


@app.get("/api/value")
def value_api():
    p = Path(WORLD) / "artifacts" / "value_report.json"
    if not p.exists():
        return {"ok": False, "error": "постройте отчёт: python -m scripts.value_report"}
    return {"ok": True, "report": json.loads(p.read_text(encoding="utf-8"))}


@app.get("/value.html", response_class=HTMLResponse)
def value_page():
    return (STATIC / "value.html").read_text(encoding="utf-8")
