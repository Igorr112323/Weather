import numpy as np


def physical_checks(df):
    out = []
    if len(df) == 0:
        return out
    p = df[["p0", "p1", "p2"]].to_numpy(float)
    out.append(("prob_sum", bool((np.abs(p.sum(axis=1) - 1.0) < 1e-5).mean() >= 0.999)))
    q = df[["q10", "q50", "q90"]].to_numpy(float)
    out.append(("q_order", bool(((q[:, 0] <= q[:, 1] + 1e-9) & (q[:, 1] <= q[:, 2] + 1e-9)).mean() >= 0.999)))
    out.append(("obs_range", bool((df["obs_z"].abs() < 5.0).mean() >= 0.999)))
    in80 = (df["obs_z"] >= df["q10"] - 1e-6) & (df["obs_z"] <= df["q90"] + 1e-6)
    out.append(("p80_coverage", round(float(in80.mean()), 3)))
    return out


def surface_budget_checks(t_air, tp, ice, doys, lat=45.0, water_tol=0.5, t_range=(250.0, 320.0)):
    from agrocast.phys.surface import SoilSnowColumn, CAP
    t_air = np.asarray(t_air, float)
    tp = np.asarray(tp, float)
    ice = np.asarray(ice, float)
    doys = np.asarray(doys, float)
    c = SoilSnowColumn(lat)
    w0_init = float(c.W @ c.dz)
    swe_init = c.swe
    p_total = 0.0
    n = len(t_air)
    w_min, w_max = np.inf, -np.inf
    t_min, t_max = np.inf, -np.inf
    bad_bounds = 0
    bad_swe = 0
    for i in range(n):
        if not np.isfinite(t_air[i]):
            continue
        c.step_day(t_air[i], tp[i] if np.isfinite(tp[i]) else 0.0, ice[i], int(doys[i]))
        p_total += max(0.0, float(tp[i])) if np.isfinite(tp[i]) else 0.0
        wv = c.W @ c.dz
        w_min = min(w_min, wv)
        w_max = max(w_max, wv)
        if np.any(c.W < -1e-9) or np.any(c.W > CAP + 1e-6):
            bad_bounds += 1
        if c.swe < -1e-9:
            bad_swe += 1
        for t in c.T:
            if not np.isfinite(t):
                bad_bounds += 1
                break
        t_min = min(t_min, float(np.min(c.T)))
        t_max = max(t_max, float(np.max(c.T)))
    w0_fin = float(c.W @ c.dz)
    dw = (w0_fin - w0_init) * 1000.0
    dswe = c.swe - swe_init
    outputs = c.runoff_c + c.et_c + c.drain_out + c.cap_loss
    water_resid = p_total - (dw + dswe + outputs)
    water_ok = abs(water_resid) <= max(water_tol, 0.05 * p_total + 1.0)
    t_ok = (t_min >= t_range[0]) and (t_max <= t_range[1])
    bounds_ok = bad_bounds == 0 and bad_swe == 0
    clip_frac = c.clip_count / max(1, n)
    return {
        "water_resid_mm": round(float(water_resid), 3),
        "water_balance": bool(water_ok),
        "p_total_mm": round(float(p_total), 1),
        "outputs_mm": round(float(outputs), 1),
        "dw_mm": round(float(dw), 1),
        "dswe_mm": round(float(dswe), 1),
        "t_range_K": (round(float(t_min), 2), round(float(t_max), 2)),
        "t_bounds_ok": bool(t_ok),
        "w_bounds_ok": bool(bounds_ok),
        "clip_frac": round(float(clip_frac), 4),
        "energy_resid_max_j_m2": round(float(c.energy_resid_max), 1),
        "n_days": int(n),
    }


def absurd_checks(df):
    out = []
    summer_t = df[(df["variable"] == "t2m") & (df["target_month"].isin([6, 7, 8]))]
    if len(summer_t):
        out.append(("summer_t2m_extreme", bool((summer_t["obs_z"].abs() < 4.0).mean() >= 0.99)))
    winter_t = df[(df["variable"] == "t2m") & (df["target_month"].isin([12, 1, 2]))]
    if len(winter_t):
        out.append(("winter_t2m_extreme", bool((winter_t["obs_z"] < 3.5).mean() >= 0.95)))
    return out
