import json
import math


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


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
