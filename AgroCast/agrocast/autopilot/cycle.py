import json
import os
import time
from pathlib import Path

import pandas as pd

from agrocast.core.config import Config
from agrocast.ingest.registry import Registry
from agrocast.ingest import openobs
from agrocast.ingest.stations import STATION_CATALOG
from agrocast.features.dataset import PointDataset
from agrocast.backtest.engine import run_backtest
from agrocast.forecast.orchestrator import forecast_point


def incremental_update(config):
    reg = Registry(config.registry_path)
    limit = openobs._last_complete_period()
    out = {}
    try:
        out["fields"] = openobs.fetch_fields(config)
    except Exception as exc:
        out["fields"] = {"error": str(exc)[:160]}
    try:
        out["soil"] = openobs.fetch_soil(config)
    except Exception as exc:
        out["soil"] = {"error": str(exc)[:160]}
    try:
        out["sst"] = openobs.fetch_ersst(config, start_year=limit.year - 1)
    except Exception as exc:
        out["sst"] = {"error": str(exc)[:160]}
    try:
        out["daily"] = openobs.fetch_cpc_daily(config, start_year=limit.year)
    except Exception as exc:
        out["daily"] = {"error": str(exc)[:160]}
    for st in STATION_CATALOG:
        try:
            from agrocast.ingest.stations import update_station

            r = update_station(config, st["id"], start_date="1991-01-01")
            out[f"station_{st['id']}"] = r.get("rows")
        except Exception as exc:
            out[f"station_{st['id']}"] = str(exc)[:120]
    reg.log_event("ingest", f"incremental update {json.dumps({k: v for k, v in out.items() if not str(k).startswith('station')}, default=str)[:400]}")
    return out


def verify_past(config):
    reg = Registry(config.registry_path)
    store = openobs.ZarrStore(config.zarr_dir)
    cache = {}
    rows = []
    for f in reg.forecasts():
        try:
            payload = json.loads(f["payload"])
        except Exception:
            continue
        key = (round(f["lat"], 4), round(f["lon"], 4))
        if key not in cache:
            try:
                cache[key] = PointDataset(config, f["lat"], f["lon"], store).raw_monthly()
            except Exception:
                cache[key] = None
        monthly = cache[key]
        if monthly is None:
            continue
        for m in payload.get("months", []) + payload.get("seasons", []):
            for pstr in ([m["month"]] if "month" in m else []) + m.get("months", []):
                pass
            p = pd.Period(f"{m['year']}-{m['month']:02d}", "M") if "month" in m else pd.Period(m["months"][0], "M")
            span = m.get("months", [str(p)])
            for v in ["t2m", "tp"]:
                if v not in m or v not in monthly.columns or "_calc" not in m.get(v, {}):
                    continue
                obs_months = [q for q in pd.period_range(p, periods=len(span), freq="M") if q in monthly.index]
                if not obs_months:
                    continue
                if v == "tp":
                    x = float(monthly.loc[obs_months, v].sum())
                else:
                    x = float(monthly.loc[obs_months, v].mean())
                calc = m[v]["_calc"]
                z = (x - calc["mu"]) / calc["sd"]
                obs_t = 0 if z < calc["e1"] else (1 if z <= calc["e2"] else 2)
                rows.append(
                    {
                        "forecast_id": f["id"],
                        "variable": v,
                        "target_year": int(m["year"]),
                        "target_month": int(p.month),
                        "lead": int(m.get("lead", 1)),
                        "p0": calc["P"][0],
                        "p1": calc["P"][1],
                        "p2": calc["P"][2],
                        "obs_tercile": obs_t,
                    }
                )
    if rows:
        reg.save_scores(rows)
    reg.log_event("verify", f"scores_saved={len(rows)}")
    return len(rows)


def monthly_cycle(config, full=False):
    reg = Registry(config.registry_path)
    reg.log_event("cycle", "start")
    if full:
        ingest = openobs.update_all(config)
    else:
        ingest = incremental_update(config)
    n = verify_past(config)
    rec_m = run_backtest(config, mode="monthly")
    rec_s = run_backtest(config, mode="seasonal")
    subs = []
    for s in reg.subscriptions():
        try:
            mode = s.get("mode") or "seasonal"
            fc = forecast_point(config, s["lat"], s["lon"], horizon=s["horizon"], variables=s["variables"], save=True, mode=mode)
            subs.append({"name": s["name"], "mode": mode, "start": fc["start"], "blocks": len(fc.get("months", fc.get("seasons", [])))})
        except Exception as exc:
            reg.log_event("sub_fail", f"{s['name']}: {exc}")
    rebuilt = _refresh_region_fields(reg)
    summary = {
        "ingest_keys": list(ingest.keys()),
        "verified_scores": n,
        "backtest_rows": {"monthly": 0 if rec_m is None else len(rec_m), "seasonal": 0 if rec_s is None else len(rec_s)},
        "subscriptions": subs,
        "region_fields_rebuilt": rebuilt,
    }
    reg.log_event("cycle", f"done {json.dumps(summary, default=str)[:400]}")
    return summary


def _refresh_region_fields(reg):
    from agrocast.core.timeutils import next_occurrence
    from agrocast.serve import region as region_mod

    base = Path(__file__).resolve().parent.parent.parent
    world_dir = os.environ.get("AGROCAST_WORLD", str(base / "world"))
    data_root = os.environ.get("AGROCAST_DATA", str(base / "data"))
    start = str(next_occurrence((pd.Timestamp.now().month % 12) + 1))
    rebuilt = []
    for rid in list(region_mod.REGIONS):
        try:
            age = region_mod.field_age_s(data_root, rid)
            if age is not None and age < region_mod.REGION_REBUILD_AFTER_S:
                continue
            region_mod.build_field(start, world_dir, data_root, region=rid, workers=2)
            rebuilt.append(rid)
            reg.log_event("region", f"{rid}: поле пересчитано, старт {start}")
        except Exception as exc:
            reg.log_event("region_fail", f"{rid}: {exc}")
    return rebuilt


def schedule(config):
    try:
        from apscheduler.schedulers.blocking import BlockingScheduler
        from apscheduler.triggers.cron import CronTrigger

        sched = BlockingScheduler()
        sched.add_job(lambda: monthly_cycle(config), CronTrigger(day=8, hour=3, minute=0))
        sched.start()
    except ImportError:
        while True:
            monthly_cycle(config)
            time.sleep(30 * 86400)
