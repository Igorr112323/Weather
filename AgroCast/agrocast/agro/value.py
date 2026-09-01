import numpy as np

RATIOS = np.round(np.arange(0.1, 0.95, 0.05), 2)


def _ev_curve(prob, event, ratios=RATIOS):
    prob = np.asarray(prob, float)
    event = np.asarray(event, bool)
    n = len(event)
    base_rate = float(event.mean())
    out = []
    for r in ratios:
        r = float(r)
        act = prob >= r
        exp_fc = (act & event).mean() * r + (act & ~event).mean() * r + (~act & event).mean() * 1.0
        exp_always = r
        exp_never = base_rate
        ref = min(exp_always, exp_never)
        ev = (ref - exp_fc) / ref if ref > 1e-9 else 0.0
        out.append((r, float(ev)))
    clim_prob = np.full(n, base_rate)
    clim_out = []
    for r, _ in out:
        r = float(r)
        act = clim_prob >= r
        exp_fc = (act & event).mean() * r + (act & ~event).mean() * r + (~act & event).mean() * 1.0
        ref = min(r, base_rate)
        ev = (ref - exp_fc) / ref if ref > 1e-9 else 0.0
        clim_out.append(ev)
    return out, clim_out


def value_summary(prob, event, label):
    curve, clim = _ev_curve(prob, event)
    arr = np.array([v for _, v in curve])
    clim_arr = np.array(clim)
    best_r = curve[int(arr.argmax())][0] if len(curve) else None
    return {
        "label": label,
        "n": int(len(event)),
        "event_rate": round(float(np.mean(event)), 3),
        "ev_max": round(float(arr.max()), 3),
        "ev_max_at_cost_loss": float(best_r),
        "ev_mean": round(float(arr.mean()), 3),
        "clim_ev_max": round(float(clim_arr.max()), 3),
        "curve": [(round(r, 2), round(v, 3), round(c, 3)) for (r, v), c in zip(curve, clim)],
    }
