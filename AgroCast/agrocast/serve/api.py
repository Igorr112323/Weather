from typing import Optional, List
import pandas as pd
from fastapi import FastAPI
from pydantic import BaseModel, Field

from agrocast.core.config import Config
from agrocast.core.timeutils import next_occurrence, month_period
from agrocast.forecast.orchestrator import forecast_point
from agrocast.store.zarrstore import ZarrStore
from agrocast.ingest.registry import Registry

app = FastAPI(title="AgroCast Engine", version="0.1.0")


class ForecastRequest(BaseModel):
    lat: float
    lon: float
    start_month: int
    horizon: int = 3
    year: Optional[int] = None
    variables: List[str] = Field(default_factory=lambda: ["t2m", "tp"])
    mode: str = "monthly"
    season_len: int = 3


class SubscriptionRequest(BaseModel):
    name: str
    lat: float
    lon: float
    horizon: int = 3
    variables: List[str] = Field(default_factory=lambda: ["t2m", "tp"])
    mode: str = "seasonal"


def get_config():
    import os
    from pathlib import Path

    data_dir = os.environ.get("AGROCAST_DATA_DIR")
    if data_dir:
        cfg_path = Path(data_dir) / "config.json"
        if cfg_path.exists():
            return Config.load(cfg_path)
        cfg = Config(data_dir=data_dir)
        return cfg
    return Config()


@app.get("/health")
def health():
    cfg = get_config()
    store = cfg.zarr_store()
    reg = Registry(cfg.registry_path)
    out = {"datasets": {}, "events": reg.recent_events(5)}
    for name in ["daily_region", "sst", "fields_monthly"]:
        out["datasets"][name] = str(store.last_time(name)) if store.exists(name) else None
    return out


@app.post("/forecast")
def forecast(req: ForecastRequest):
    cfg = get_config()
    start = month_period(req.year, req.start_month) if req.year else next_occurrence(req.start_month)
    horizon = max(1, min(int(req.horizon), cfg.horizon_max))
    mode = req.mode if req.mode in ("monthly", "seasonal") else "monthly"
    return forecast_point(cfg, req.lat, req.lon, start=start, horizon=horizon, variables=tuple(req.variables), mode=mode, season_len=req.season_len)


@app.post("/subscriptions")
def add_subscription(req: SubscriptionRequest):
    cfg = get_config()
    reg = Registry(cfg.registry_path)
    reg.add_subscription(req.name, req.lat, req.lon, req.horizon, tuple(req.variables), mode=req.mode)
    return {"status": "added"}


@app.get("/subscriptions")
def list_subscriptions():
    cfg = get_config()
    reg = Registry(cfg.registry_path)
    return reg.subscriptions()


@app.post("/update")
def update():
    from agrocast.autopilot.cycle import monthly_cycle

    cfg = get_config()
    return monthly_cycle(cfg)
