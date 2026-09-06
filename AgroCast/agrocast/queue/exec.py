import argparse
import json
import os
import sys

EXIT_RESULT_OK = 0
EXIT_RESULT_ERROR = 1
EXIT_CONFIG = 78

_BUILTINS = {}


def emit(event, **fields):
    line = {"event": event, **fields}
    sys.stdout.write(json.dumps(line, ensure_ascii=False, allow_nan=False) + "\n")
    sys.stdout.flush()


def builtin(kind):
    def register(func):
        _BUILTINS[kind] = func
        return func
    return register


def load_handlers():
    handlers = dict(_BUILTINS)
    module_name = os.environ.get("AGROCAST_QUEUE_EXECUTOR_MODULE")
    if module_name:
        import importlib

        extra = importlib.import_module(module_name)
        for kind, func in getattr(extra, "HANDLERS", {}).items():
            if kind in {"noop", "sleep"} or kind not in handlers:
                handlers[kind] = func
    return handlers


@builtin("point_forecast")
def run_point_forecast(params, log):
    from agrocast.forecast.orchestrator import forecast_point
    from agrocast.serve.pipeline import ensure_point, point_config

    world_dir = params["world_dir"]
    data_root = params["data_root"]
    spec = params["spec"]
    cfg, _ = point_config(world_dir, data_root, float(spec["lat"]), float(spec["lon"]), params.get("config_snapshot"))
    ensure_point(cfg, world_dir, log)
    log("считаю прогноз")
    payload = forecast_point(
        cfg, float(spec["lat"]), float(spec["lon"]),
        start=spec["start"], horizon=int(spec["horizon"]), mode=spec["mode"],
        season_len=int(spec["season_len"]), save=False,
        variables=tuple(spec.get("variables") or ("t2m", "tp")),
        variety=spec.get("variety_name") or "",
    )
    if payload.get("start") != spec["start"]:
        raise ValueError("forecast payload does not match the requested start")
    return payload


@builtin("point_hindcast")
def run_point_hindcast(params, log):
    from agrocast.serve.pipeline import ensure_point, point_config, run_hindcast

    world_dir = params["world_dir"]
    data_root = params["data_root"]
    spec = params["spec"]
    cfg, _ = point_config(world_dir, data_root, float(spec["lat"]), float(spec["lon"]), params.get("config_snapshot"))
    ensure_point(cfg, world_dir, log)
    return run_hindcast(cfg, float(spec["lat"]), float(spec["lon"]), spec["start"], spec["mode"], int(spec["horizon"]), log)


@builtin("region_cell")
def run_region_cell(params, log):
    from agrocast.forecast.orchestrator import forecast_point
    from agrocast.core.contracts import target_months
    from agrocast.serve.pipeline import ensure_point, point_config

    world_dir = params["world_dir"]
    data_root = params["data_root"]
    cell = params["cell"]
    start = params["start"]
    cfg, _ = point_config(world_dir, data_root, float(cell["lat"]), float(cell["lon"]), params.get("config_snapshot"))
    ensure_point(cfg, world_dir, log)
    payload = forecast_point(
        cfg, float(cell["lat"]), float(cell["lon"]),
        start=start, horizon=3, mode="seasonal", season_len=3, save=False, variables=("t2m", "tp"),
    )
    item = (payload.get("seasons") or [None])[0]
    if not item or "tp" not in item:
        raise RuntimeError(f"{cell['id']}: forecast without precipitation block")
    probabilities = item["tp"]["tercile_probs"]
    months = item.get("months") or []
    if payload.get("start") != start or months != target_months(start, 3):
        raise ValueError("cell forecast does not match the requested period")
    return {
        "id": cell["id"], "lat": float(cell["lat"]), "lon": float(cell["lon"]),
        "target": months[0] if months else start, "months": months,
        "below": float(probabilities["below"]), "normal": float(probabilities["normal"]),
        "above": float(probabilities["above"]),
        "p50_mm": (item["tp"].get("quantiles_mm") or {}).get("p50"),
        "normal_mm": item["tp"].get("normal_mm"),
        "issue_through": payload.get("issue_data_through"),
    }


@builtin("region_merge")
def run_region_merge(params, log):
    from agrocast.serve.region import kriging_merge

    return kriging_merge(
        params["grid"], params["cells"], params["start"], params["region"],
        float(params.get("runtime_forecast_s") or 0.0), log,
    )


@builtin("noop")
def run_noop(params, log):
    for line in params.get("log") or []:
        log(str(line))
    if params.get("sleep_seconds"):
        import time

        time.sleep(float(params["sleep_seconds"]))
    if params.get("fail"):
        raise RuntimeError(str(params["fail"]))
    return {"ok": True, "echo": params.get("echo"), "threads": os.environ.get("OMP_NUM_THREADS")}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="agrocast-queue-exec")
    parser.add_argument("--input", required=True)
    args = parser.parse_args(argv)
    with open(args.input, "r", encoding="utf-8") as handle:
        spec = json.load(handle)
    blas_threads = int(spec.get("blas_threads") or 1)
    from agrocast.queue.sandbox import apply_limits, apply_syscall_denylist, effective_thread_report, set_no_new_privileges, verify_denylist

    for name, value in {"PYTHONHASHSEED": "0"}.items():
        os.environ[name] = value
    from agrocast.queue.sandbox import thread_limit_environment

    os.environ.update(thread_limit_environment(blas_threads))
    log_lines = {"count": 0}

    def log(message):
        log_lines["count"] += 1
        emit("log", line=str(message)[:500])

    try:
        set_no_new_privileges()
        applied = apply_syscall_denylist()
        verify = verify_denylist()
        limits = apply_limits(int(spec.get("deadline_seconds") or 1800),
                             fsize_mb=int(spec.get("fsize_mb") or 256),
                             max_rss_mb=int(spec.get("max_rss_mb") or 0))
        emit("sandbox", applied_rules=applied["applied_rules"], verified=bool(verify.get("ok")), limits=limits, threads=effective_thread_report())
    except Exception as error:
        emit("fatal", code="sandbox_unavailable", summary=f"{type(error).__name__}: {error}")
        return EXIT_CONFIG
    handlers = load_handlers()
    handler = handlers.get(spec["kind"])
    if handler is None:
        emit("error", code="unknown_kind", summary=f"no executor registered for kind {spec['kind']}")
        return EXIT_RESULT_ERROR
    try:
        payload = handler(spec.get("params") or {}, log)
    except Exception as error:
        emit("error", code="compute_failed", summary=f"{type(error).__name__}: {error}"[:500])
        return EXIT_RESULT_ERROR
    import hashlib

    source = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    checksum = hashlib.sha256(source.encode("utf-8")).hexdigest()
    result_path = spec["result_path"]
    os.makedirs(os.path.dirname(result_path), exist_ok=True)
    with open(result_path, "w", encoding="utf-8") as handle:
        handle.write(source)
        handle.flush()
        os.fsync(handle.fileno())
    emit("result", checksum=checksum, size_bytes=len(source.encode("utf-8")))
    return EXIT_RESULT_OK


if __name__ == "__main__":
    sys.exit(main())
