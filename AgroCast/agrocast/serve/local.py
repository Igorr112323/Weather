import hashlib
import json
import time

import pandas as pd
from pathlib import Path

from agrocast.core.contracts import CONTRACT_VERSION
from agrocast.core.jsoncodec import strict_json
from agrocast.bundle.releases import release_status
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


def use_active_bundle(settings):
    from agrocast.bundle.releases import active_release

    info = active_release(Path(settings.bundles_dir))
    if info is None:
        return settings, {"status": "none", "releases_root": str(settings.bundles_dir)}
    if info["problems"]:
        raise ValueError("active bundle release is corrupt: " + "; ".join(info["problems"][:5]))
    return settings.with_paths(world_dir=info["path"]), {"status": "ok", "release_id": info["release_id"], "path": info["path"]}


def _read_json_object(path):
    try:
        value = strict_json(Path(path).read_bytes())
    except (OSError, ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _bundle_manifest(world_dir):
    ready = _read_json_object(Path(world_dir) / "ready.json")
    config = _read_json_object(Path(world_dir) / "config.json")
    release = _read_json_object(Path(world_dir) / "release.json")
    manifest = {"ready": ready, "configuration": config}
    if isinstance(release, dict):
        manifest["release"] = {
            "id": release.get("id"),
            "digest": release.get("digest"),
            "created_utc": release.get("created_utc"),
            "files": len(release.get("files", [])),
            "total_bytes": release.get("total_bytes"),
        }
    for source in (Path(world_dir) / "registry", Path(world_dir) / "registry.json", Path(world_dir) / "manifest.json"):
        candidate = _read_json_object(source)
        if candidate is not None:
            manifest["dates"] = {key: value for key, value in candidate.items() if isinstance(value, (int, str))}
            break
    return manifest


def current_releases_manifest(settings):
    world = Path(settings.world_dir)
    ready = world / "ready.json"
    config = world / "config.json"
    release = world / "release.json"
    if release.is_file():
        data_seed = release.read_bytes()
    elif ready.is_file():
        data_seed = ready.read_bytes()
    else:
        data_seed = json.dumps(_list_directory(world), sort_keys=True).encode("utf-8")
    return {
        "data_release": _digest_bytes(data_seed),
        "model_release": _digest_bytes(config.read_bytes()) if config.is_file() else _digest_bytes(data_seed),
        "application_release": _digest_bytes(CONTRACT_VERSION.encode("utf-8")),
    }


def ensure_releases(settings):
    from agrocast.store.atomic import write_json

    path = Path(settings.release_manifest_file) if settings.release_manifest_file is not None else settings.state_dir / DESKTOP_RELEASES_FILE
    if settings.release_manifest_file is None:
        manifest = current_releases_manifest(settings)
        stored = _read_json_object(path)
        if stored != manifest:
            path.parent.mkdir(parents=True, exist_ok=True)
            write_json(path, manifest)
    return Releases.from_file(path)


def _sources_through(settings):
    from agrocast.serve import readiness

    out = {}
    try:
        store = settings.compute_config().zarr_store()
    except Exception:
        return {name: None for name in (*readiness.REQUIRED_SOURCES, *readiness.OPTIONAL_SOURCES)}
    now = pd.Timestamp.now()
    for name in (*readiness.REQUIRED_SOURCES, *readiness.OPTIONAL_SOURCES):
        block = readiness._source_block(store, name, now)
        out[name] = block["last"]
    return out


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
    settings = use_active_bundle(settings)[0]
    return {
        "generated_at": int(time.time()),
        "state_dir": str(settings.state_dir),
        "world_dir": str(settings.world_dir),
        "bundle_release": release_status(settings.bundles_dir),
        "bundle": _list_directory(settings.world_dir),
        "bundle_manifest": _bundle_manifest(settings.world_dir),
        "observations": _list_directory(settings.state_dir / "compute"),
        "results_cache": {"entries": cache_entries, "total": len(list(cache_root.glob('*.json'))) if cache_root.is_dir() else 0},
        "releases": releases,
        "sources_through": _sources_through(settings),
    }


def _local_identity(settings, spec, releases, principal):
    from agrocast.queue.admission import _queue_settings

    return ResultIdentity.point(spec, releases, _queue_settings(settings), CacheScope.for_principal(principal), variety=None)


def _crop_db(settings):
    from agrocast.crops.db import CropDB

    return CropDB(settings.state_dir / "crops.db", str(Path(settings.world_dir) / "artifacts" / "crop_seed.json"))


def list_local_crops(settings):
    settings = use_active_bundle(settings)[0]
    db = _crop_db(settings)
    return db.all()


def upsert_local_crop(settings, data):
    settings = use_active_bundle(settings)[0]
    db = _crop_db(settings)
    return db.upsert(data)


def delete_local_crop(settings, name):
    settings = use_active_bundle(settings)[0]
    db = _crop_db(settings)
    return db.remove(name)


def get_local_crop(settings, name):
    settings = use_active_bundle(settings)[0]
    db = _crop_db(settings)
    return db.get(name)


def run_forecast(settings, spec, principal):
    from agrocast.forecast.orchestrator import forecast_point
    from agrocast.serve.pipeline import ensure_point, point_config

    settings = use_active_bundle(settings)[0]
    releases = ensure_releases(settings)
    variety = getattr(spec, "variety", None) or getattr(spec, "variety_name", None) or ""
    if isinstance(variety, str):
        variety = variety.strip()
    else:
        variety = ""
    identity = _local_identity(settings, spec, releases, principal)
    cache = ResultCache(settings.state_dir)
    hit = cache.read(identity)
    if hit is not None and not variety:
        return {
            "cached": True, "payload": hit.payload, "identity": identity.model_dump(mode="json"),
            "computed_at": None, "log": ["результат взят из локального кэша — расчёт не запускался"],
        }
    log_lines = []
    cfg, _ = point_config(settings.world_dir, settings.state_dir, float(spec.lat), float(spec.lon), settings.compute_config().to_dict())
    ensure_point(cfg, settings.world_dir, lambda message: log_lines.append(str(message)[:500]))
    payload = forecast_point(
        cfg, float(spec.lat), float(spec.lon), start=spec.start, horizon=int(spec.horizon),
        mode=spec.mode, season_len=int(spec.season_len), save=False, variables=("t2m", "tp"), variety=variety,
    )
    if payload.get("start") != spec.start:
        raise ValueError("forecast payload does not match the requested start")
    if not payload.get("seasons"):
        raise ValueError("forecast payload has no season block")
    if not variety:
        cache.write(identity, payload)
    return {
        "cached": False, "payload": payload, "identity": identity.model_dump(mode="json"),
        "computed_at": int(time.time()), "log": log_lines[-40:],
    }
