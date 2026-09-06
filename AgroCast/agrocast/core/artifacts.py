import json
from pathlib import Path

BLENDER_SCHEMA = "blender-v2"
STACK_SCHEMA = "stack-v1"
CALIB_SCHEMA = "calib-v1"
CONFORMAL_SCHEMA = "conformal-v1"


class ArtifactError(ValueError):
    pass


def read_artifact(path, schema=None, name="artifact", known_models=None):
    p = Path(path)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
    except (ValueError, OSError) as exc:
        raise ArtifactError(f"{name} artifact at {p.name} is not readable: {type(exc).__name__}") from exc
    if not isinstance(data, dict):
        raise ArtifactError(f"{name} artifact at {p.name} must be a JSON object")
    got = data.get("schema")
    if got is not None and schema is not None and got != schema:
        raise ArtifactError(f"{name} artifact schema {got!r} is not supported (expected {schema!r} or no tag)")
    models = data.get("models")
    if models is not None:
        if not isinstance(models, list) or not all(isinstance(m, str) for m in models):
            raise ArtifactError(f"{name} artifact carries a malformed model set")
        if known_models is not None:
            unknown = sorted(set(models) - set(known_models))
            if unknown:
                raise ArtifactError(f"{name} artifact references unknown models {unknown}")
    return data


def write_artifact(path, payload, schema, models=None):
    body = dict(payload)
    body["schema"] = schema
    if models is not None:
        body["models"] = sorted(set(str(m) for m in models))
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(body, indent=2, sort_keys=True))
    return p


def check_weight_models(weights, known_models, name="blender"):
    if known_models is None:
        return
    used = set()
    for by_season in (weights or {}).values():
        for by_model in (by_season or {}).values():
            used |= set((by_model or {}).keys())
    unknown = sorted(used - set(known_models))
    if unknown:
        raise ArtifactError(f"{name} artifact references models {unknown} that are not in the current model set")
