import json
import math


def _normalize(value):
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            text = key if isinstance(key, str) else str(key)
            if text in out:
                raise ValueError("duplicate normalized JSON key")
            out[text] = _normalize(item)
        return out
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    return value


def canonical_json(value):
    return json.dumps(_normalize(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def strict_json(value):
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = item
        return result

    def nonfinite(value):
        raise ValueError("non-finite JSON number")

    def number(text):
        parsed = float(text)
        if not math.isfinite(parsed):
            raise ValueError("non-finite JSON number")
        return parsed

    return json.loads(value, object_pairs_hook=pairs, parse_constant=nonfinite, parse_float=number)
