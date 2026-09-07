MM_TO_M3_PER_HA = 10.0


def mm_to_m3_ha(mm, integer=True):
    if mm is None:
        return None
    try:
        v = float(mm)
    except (TypeError, ValueError):
        return None
    if v != v:
        return None
    v = max(0.0, v) * MM_TO_M3_PER_HA
    return int(round(v)) if integer else float(v)


def m3_ha_to_mm(m3):
    if m3 is None:
        return None
    try:
        v = float(m3)
    except (TypeError, ValueError):
        return None
    if v != v:
        return None
    return max(0.0, v) / MM_TO_M3_PER_HA


def decade_share(shares, total_mm):
    if total_mm is None or total_mm <= 0:
        return [0.0] * len(shares)
    s = float(sum(shares))
    if s <= 0:
        return [0.0] * len(shares)
    return [float(x) / s * float(total_mm) for x in shares]
