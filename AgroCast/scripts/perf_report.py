import json
import os
import platform
import resource
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))


def _usage(before):
    after = resource.getrusage(resource.RUSAGE_SELF)
    cpu = (after.ru_utime + after.ru_stime) - (before[0] + before[1])
    return round(cpu, 1), int(after.ru_maxrss / 1024)


def _mark():
    r = resource.getrusage(resource.RUSAGE_SELF)
    return (r.ru_utime, r.ru_stime)


def measure_point(world, state):
    from agrocast.core.contracts import ForecastSpec
    from agrocast.core.settings import RuntimeSettings
    from agrocast.serve.local import run_forecast
    from agrocast.serve.runtime import _desktop_principal

    settings = RuntimeSettings(world_dir=world, state_dir=state)
    principal = _desktop_principal()
    spec = ForecastSpec.model_validate({
        "kind": "forecast", "lat": 45.0311, "lon": 39.0722, "start": "2025-12", "horizon": 3,
        "season_len": 3, "mode": "seasonal", "variables": ["t2m", "tp"],
    })
    run_forecast(settings, spec, principal)
    for entry in (state / "results-v1").glob("*.json"):
        entry.unlink()
    results = {}
    started = time.perf_counter()
    mark = _mark()
    first = run_forecast(settings, spec, principal)
    results["cold_seconds"] = round(time.perf_counter() - started, 1)
    results["cold_cpu_seconds"], results["rss_peak_mb"] = _usage(mark)
    started = time.perf_counter()
    mark = _mark()
    second = run_forecast(settings, spec, principal)
    results["warm_seconds"] = round(time.perf_counter() - started, 2)
    results["warm_cpu_seconds"], _ = _usage(mark)
    results["cached_hit"] = bool(second.get("cached"))
    results["payload_ok"] = bool(first.get("payload", {}).get("seasons"))
    return results


def measure_field(world, state):
    from agrocast.serve.local import ensure_releases
    from agrocast.serve import region as region_mod
    from agrocast.core.settings import RuntimeSettings

    releases = ensure_releases(RuntimeSettings(world_dir=world, state_dir=state))
    started = time.perf_counter()
    mark = _mark()
    field = region_mod.build_field("2025-12", world, state, workers=2, releases=releases)
    wall = time.perf_counter() - started
    cpu, rss = _usage(mark)
    points = field.get("points") if isinstance(field, dict) else None
    return {
        "wall_seconds": round(wall, 1),
        "cpu_seconds": cpu,
        "rss_peak_mb": rss,
        "cells": len(points) if isinstance(points, list) else field.get("meta", {}).get("n_cells") if isinstance(field, dict) else None,
        "ok": bool(field) if isinstance(field, dict) else True,
    }


def main(argv=None):
    world = Path(argv[0] if argv else BASE / "world").resolve()
    state = Path(argv[1] if len(argv or ()) > 1 else "/tmp/agrocast-perf-state").resolve()
    os.makedirs(state, exist_ok=True)
    out = {
        "bench": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "cpus": os.cpu_count(),
            "machine": platform.machine(),
        },
        "point": measure_point(world, state),
    }
    try:
        out["field28"] = measure_field(world, state)
    except Exception as error:
        out["field28"] = {"error": str(error)[:300]}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
