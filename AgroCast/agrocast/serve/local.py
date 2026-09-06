import hashlib
import json
import os
import time
from pathlib import Path

from agrocast.core.contracts import CONTRACT_VERSION
from agrocast.core.jsoncodec import strict_json
from agrocast.store.results import CacheScope, Releases, ResultCache, ResultIdentity

LIST_LIMIT = 400
DESKTOP_RELEASES_FILE = "desktop-releases.json"


def _digest_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _list_directory(root):
    files = []
    count = 0
    total = 0
    if root.is_dir():
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            count += 1
            try:
                info = path.stat()
            except OSError:
                continue
            total += info.st_size
            if len(files) < LIST_LIMIT:
                files.append({"path": str(path.relative_to(root)), "size_bytes": info.st_size, "modified_at": int(info.st_mtime)})
    return {"files": files, "file_count": count, "total_bytes": total, "truncated": count > LIST_LIMIT}


def _read_json_object(path):
    try:
        value = strict_json(Path(path).read_bytes())
    except (OSError, ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _bundle_manifest(world_dir):
    ready = _read_json_object(Path(world_dir) / "ready.json")
    config = _read_json_object(Path(world_dir) / "config.json")
    manifest = {"ready": ready, "configuration": config}
    for source in (Path(world_dir) / "registry", Path(world_dir) / "registry.json", Path(world_dir) / "manifest.json"):
        candidate = _read_json_object(source)
        if candidate is not None:
            manifest["dates"] = {key: value for key, value in candidate.items() if isinstance(value, (int, str))}
            break
    return manifest


def ensure_releases(settings):
    path = Path(settings.release_manifest_file) if settings.release_manifest_file is not None else settings.state_dir / DESKTOP_RELEASES_FILE
    if not path.exists():
        world_files = _list_directory(settings.world_dir)
        ready = Path(settings.world_dir) / "ready.json"
        config = Path(settings.world_dir) / "config.json"
        data_seed = ready.read_bytes() if ready.is_file() else json.dumps(world_files, sort_keys=True).encode("utf-8")
        manifest = {
            "data_release": _digest_bytes(data_seed),
            "model_release": _digest_bytes(config.read_bytes()) if config.is_file() else _digest_bytes(data_seed),
            "application_release": _digest_bytes(CONTRACT_VERSION.encode("utf-8")),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, sort_keys=True)
    return Releases.from_file(path)


def collect_inputs(settings):
    cache_root = settings.state_dir / "results-v1"
    cache_entries = []
    if cache_root.is_dir():
        paths = sorted(cache_root.glob("*.json"), key=lambda item: item.name)
        for index, path in enumerate(paths[:LIST_LIMIT]):
            envelope = _read_json_object(path)
            info = path.stat()
            cache_entries.append({
                "key": path.stem, "size_bytes": info.st_size, "stored_at": envelope.get("created_at") if envelope else None,
                "payload_sha256": envelope.get("payload_sha256") if envelope else None,
            })
        if len(paths) > LIST_LIMIT:
            cache_entries.append({"key": "…", "size_bytes": 0, "stored_at": None, "payload_sha256": "truncated"})
    releases_path = Path(settings.release_manifest_file) if settings.release_manifest_file is not None else settings.state_dir / DESKTOP_RELEASES_FILE
    releases = None
    if releases_path.exists():
        try:
            loaded = Releases.from_file(releases_path)
            releases = loaded.model_dump(mode="json")
        except (OSError, ValueError, TypeError):
            releases = None
    return {
        "generated_at": int(time.time()),
        "state_dir": str(settings.state_dir),
        "world_dir": str(settings.world_dir),
        "bundle": _list_directory(settings.world_dir),
        "bundle_manifest": _bundle_manifest(settings.world_dir),
        "observations": _list_directory(settings.state_dir / "compute"),
        "results_cache": {"entries": cache_entries, "total": len(list(cache_root.glob('*.json'))) if cache_root.is_dir() else 0},
        "releases": releases,
    }


def _local_identity(settings, spec, releases, principal):
    from agrocast.queue.admission import _queue_settings

    return ResultIdentity.point(spec, releases, _queue_settings(settings), CacheScope.for_principal(principal), variety=None)


def run_forecast(settings, spec, principal):
    from agrocast.forecast.orchestrator import forecast_point
    from agrocast.serve.pipeline import ensure_point, point_config

    releases = ensure_releases(settings)
    identity = _local_identity(settings, spec, releases, principal)
    cache = ResultCache(settings.state_dir)
    hit = cache.read(identity)
    if hit is not None:
        return {
            "cached": True, "payload": hit.payload, "identity": identity.model_dump(mode="json"),
            "computed_at": None, "log": ["результат взят из локального кэша — расчёт не запускался"],
        }
    log_lines = []
    cfg, _ = point_config(settings.world_dir, settings.state_dir, float(spec.lat), float(spec.lon), settings.compute_config().to_dict())
    ensure_point(cfg, settings.world_dir, lambda message: log_lines.append(str(message)[:500]))
    payload = forecast_point(
        cfg, float(spec.lat), float(spec.lon), start=spec.start, horizon=int(spec.horizon),
        mode=spec.mode, season_len=int(spec.season_len), save=False, variables=("t2m", "tp"), variety="",
    )
    if payload.get("start") != spec.start:
        raise ValueError("forecast payload does not match the requested start")
    if not payload.get("seasons"):
        raise ValueError("forecast payload has no season block")
    cache.write(identity, payload)
    return {
        "cached": False, "payload": payload, "identity": identity.model_dump(mode="json"),
        "computed_at": int(time.time()), "log": log_lines[-40:],
    }
