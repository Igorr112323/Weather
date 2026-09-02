import json
import os
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

app = FastAPI(title="AgroCast Россия")

JOBS = {}


class PrepareRequest(BaseModel):
    lat: float
    lon: float
    start: str = ""
    horizon: int = 3
    mode: str = "seasonal"
    season_len: int = 3
    kind: str = "forecast"
    year: int = 2020


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
        return JSONResponse({"error": "Продукт поддерживает только территорию России"}, status_code=400)
    job_id = uuid.uuid4().hex[:10]
    from agrocast.serve.pipeline import Job, start_job

    job = Job(job_id, req.model_dump())
    JOBS[job_id] = job
    start_job(job, WORLD, DATA_ROOT)
    return {"job": job_id}


@app.get("/api/job/{job_id}")
def job_state(job_id: str):
    job = JOBS.get(job_id)
    if job is None:
        return JSONResponse({"error": "задача не найдена"}, status_code=404)
    out = {"status": job.status, "log": job.log}
    if job.status == "error":
        out["error"] = job.error
    if job.status == "done":
        out["report"] = job.result
    return out


@app.post("/api/subscribe")
def subscribe(req: SubscribeRequest):
    if not inside_russia(req.lat, req.lon):
        return JSONResponse({"error": "Продукт поддерживает только территорию России"}, status_code=400)
    from agrocast.ingest.registry import Registry
    from agrocast.serve.pipeline import world_config

    cfg = world_config(WORLD)
    reg = Registry(cfg.registry_path)
    reg.add_subscription(req.name, req.lat, req.lon, req.horizon, ("t2m", "tp"), req.mode)
    return {"ok": True, "name": req.name}


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
