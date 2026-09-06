from agrocast.core.contracts import ForecastSpec, RegionFieldSpec
from agrocast.core.jsoncodec import strict_json
from agrocast.identity.credentials import IdentityError
from agrocast.serve.errors import APIError
from agrocast.serve.region import grid_path, region_identity
from agrocast.store.results import CacheScope, Releases, ResultCache, ResultIdentity, fingerprint


def _releases(settings):
    try:
        return Releases.from_file(settings.release_manifest_file)
    except (OSError, ValueError, TypeError):
        raise APIError("release_identity_unavailable", 503, "The release manifest is required to admit a computation") from None


def _queue_settings(settings):
    config = settings.compute_config().to_dict()
    for key in ("data_dir", "shared_zarr", "bundle_dir", "runtime_dir"):
        config.pop(key, None)
    return {"world_configuration": config, "queue": settings.queue_snapshot()}


def admit_point(queue, identity, settings, principal, request: ForecastSpec):
    releases = _releases(settings)
    variety = None
    variety_name = None
    if request.variety_id is not None:
        variety = identity.variety_snapshot(principal, request.variety_id, request.variety_revision)
        crop = identity.get_resource("crops", principal, str(request.variety_id))
        variety_name = (crop.get("data") or {}).get("name")
    scope = CacheScope.for_principal(principal)
    identity_model = ResultIdentity.point(
        request, releases, _queue_settings(settings), scope, variety=variety,
    )
    spec = request.model_dump(mode="json")
    if variety_name is not None:
        spec["variety_name"] = variety_name
    params = {
        "spec": spec,
        "identity": identity_model.model_dump(mode="json"),
        "releases": releases.model_dump(mode="json"),
        "config_snapshot": settings.compute_config().to_dict(),
    }
    kind = "point_hindcast" if spec["kind"] == "hindcast" else "point_forecast"
    result = queue.enqueue(principal, kind, params, dedup_sha256=identity_model.key())
    result["cached"] = ResultCache(settings.state_dir).read(identity_model) is not None
    return result


def admit_region(queue, settings, principal, request: RegionFieldSpec):
    releases = _releases(settings)
    path = grid_path(settings.world_dir, request.region.value)
    try:
        grid = strict_json(path.read_bytes())
    except (OSError, ValueError, RecursionError):
        raise APIError("grid_unavailable", 503, "The regional grid artifact cannot be read from the bundle") from None
    grid_sha256 = fingerprint(grid)
    identity_model = region_identity(request.start, settings.world_dir, request.region.value, releases)
    if identity_model.grid_sha256 != grid_sha256:
        raise APIError("grid_changed", 503, "The regional grid changed while the request was being admitted") from None
    params = {
        "start": request.start,
        "region": request.region.value,
        "grid_sha256": grid_sha256,
        "identity": identity_model.model_dump(mode="json"),
        "config_snapshot": settings.compute_config().to_dict(),
    }
    result = queue.enqueue(principal, "region_field", params, dedup_sha256=identity_model.key())
    result["cached"] = ResultCache(settings.state_dir).read(identity_model) is not None
    return result


def require_queue_service(request):
    queue = getattr(request.app.state, "queue", None)
    if queue is None:
        raise IdentityError("identity_unavailable", 503)
    return queue
