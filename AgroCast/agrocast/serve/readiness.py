import json
import time
from pathlib import Path

import pandas as pd
from sqlalchemy import text

BUNDLE_CONTRACT = "world-v1"
REQUIRED_SOURCES = ("daily_region", "fields_monthly")
OPTIONAL_SOURCES = ("sst", "strat_snow", "oisst_boxes", "regimes")
SOURCE_LIMITS = {
    "daily_region": 14,
    "sst": 120,
    "fields_monthly": 60,
    "strat_snow": 60,
    "oisst_boxes": 90,
    "regimes": 365,
}
UPDATE_SCHEDULE_DAYS = 7
SKILL_STALE_DAYS = 400


def _age_days(last, now):
    if last.tzinfo is not None:
        last = last.tz_localize(None)
    return max(0, int((now - last).total_seconds() // 86400))


def _source_block(store, name, now):
    block = {"status": "ok", "last": None, "available_at": None, "age_days": None, "limit_days": SOURCE_LIMITS[name]}
    try:
        if not store.exists(name):
            block["status"] = "missing"
            return block
        last = store.last_time(name)
        if last is None:
            block["status"] = "empty"
            return block
        age = _age_days(last, now)
        block["last"] = str(last.date())
        block["available_at"] = str(last.date())
        block["age_days"] = age
        if age > SOURCE_LIMITS[name]:
            block["status"] = "stale"
            return block
        dataset = store.open(name)
        if bool(dataset.isel(time=-1).to_array().isnull().any()):
            block["status"] = "incomplete_last_row"
        return block
    except Exception:
        block["status"] = "error"
        return block


def _bundle_block(world_dir, store, now):
    block = {"status": "ok", "contract": BUNDLE_CONTRACT, "manifest_present": False}
    try:
        raw = (Path(world_dir) / "ready.json").read_text(encoding="utf-8")
    except FileNotFoundError:
        block["status"] = "missing"
        return block
    except OSError:
        block["status"] = "corrupt"
        return block
    try:
        manifest = json.loads(raw)
    except ValueError:
        block["status"] = "corrupt"
        return block
    if not isinstance(manifest, dict) or manifest.get("ok") is not True:
        block["status"] = "not_ok"
        return block
    block["manifest_present"] = True
    contract = manifest.get("contract")
    if contract is not None and str(contract) != BUNDLE_CONTRACT:
        block["status"] = "contract_mismatch"
        block["manifest_contract"] = str(contract)
        return block
    for key in ("built_at", "predictor_through"):
        if manifest.get(key) is not None:
            block[key] = str(manifest[key])
    through = manifest.get("predictor_through")
    if through is not None:
        try:
            claimed = pd.Timestamp(str(through))
        except (TypeError, ValueError):
            block["status"] = "corrupt"
            return block
        latest = None
        for name in REQUIRED_SOURCES:
            if store.exists(name):
                stamp = store.last_time(name)
                if stamp is not None and (latest is None or stamp > latest):
                    latest = stamp
        if latest is not None and claimed.normalize() > pd.Timestamp(latest).normalize():
            block["status"] = "manifest_ahead_of_store"
    return block


def _region_block(world_dir, now):
    from agrocast.region.regions import REGIONS, grid_artifact_path, skill_artifact_path

    out = {}
    for region_id in sorted(REGIONS):
        row = {"grid": "ok", "skill": "ok", "skill_generated_at": None, "skill_age_days": None}
        grid_path = grid_artifact_path(world_dir, region_id)
        skill_path = skill_artifact_path(world_dir, region_id)
        if not grid_path.exists():
            row["grid"] = "missing"
        else:
            try:
                json.loads(grid_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                row["grid"] = "corrupt"
        if not skill_path.exists():
            row["skill"] = "missing"
        else:
            try:
                payload = json.loads(skill_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                payload = None
            if not isinstance(payload, dict):
                row["skill"] = "corrupt"
            else:
                stamp = payload.get("generated_at")
                if stamp is None:
                    row["skill"] = "undated"
                else:
                    try:
                        when = pd.Timestamp(str(stamp))
                    except (TypeError, ValueError):
                        row["skill"] = "corrupt"
                    else:
                        age = _age_days(when, now)
                        row["skill_generated_at"] = str(when.date())
                        row["skill_age_days"] = age
                        if age > SKILL_STALE_DAYS:
                            row["skill"] = "stale"
        out[region_id] = row
    return out


def _queue_block(engine):
    if engine is None:
        return {"status": "unavailable"}, []
    try:
        with engine.connect() as connection:
            counts = connection.execute(text("SELECT status, COUNT(*) FROM jobs GROUP BY status")).all()
            oldest = connection.execute(text("SELECT MIN(created_at) FROM jobs WHERE status = 'queued'")).scalar()
    except Exception:
        return {"status": "error"}, ["db_unavailable"]
    block = {"status": "ok", "jobs": {str(status): int(total) for status, total in counts}}
    if oldest is not None:
        block["oldest_queued_age_s"] = int(max(0, time.time() - int(oldest)))
    return block, []


def evaluate(settings, config, engine=None, now=None):
    now = now if isinstance(now, pd.Timestamp) else pd.Timestamp.now()
    reasons = []
    degraded = []
    store = config.zarr_store()
    sources = {}
    for name in (*REQUIRED_SOURCES, *OPTIONAL_SOURCES):
        block = _source_block(store, name, now)
        sources[name] = block
        if block["status"] != "ok":
            tag = f"{name}:{block['status']}"
            if name in REQUIRED_SOURCES:
                reasons.append(tag)
            else:
                degraded.append(tag)
    bundle = _bundle_block(settings.world_dir, store, now)
    if bundle["status"] != "ok":
        reasons.append(f"bundle:{bundle['status']}")
    regions = _region_block(settings.world_dir, now)
    for region_id, row in regions.items():
        if row["grid"] != "ok":
            reasons.append(f"region/{region_id}:grid_{row['grid']}")
        if row["skill"] in ("missing", "corrupt"):
            reasons.append(f"region/{region_id}:skill_{row['skill']}")
        if row["skill"] in ("stale", "undated"):
            degraded.append(f"region/{region_id}:skill_{row['skill']}")
    queue, queue_reasons = _queue_block(engine)
    reasons.extend(queue_reasons)
    return {
        "status": "ready" if not reasons else "not_ready",
        "reasons": reasons,
        "degraded": degraded,
        "sources": sources,
        "bundle": bundle,
        "regions": regions,
        "queue": queue,
    }
