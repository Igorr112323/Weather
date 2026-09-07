from agrocast.blend.blender import Blender, season_of
from agrocast.blend.calibration import TercileCalibrator
from agrocast.blend.conformal import ConformalQuantileCalibrator
from agrocast.blend.nn_stack import load_alpha
from agrocast.blend.regime_clim import SPECS, RegimeClimatology, memory_z, tercile_grid
from agrocast.core.mathutils import exp_weights
from agrocast.core.policy import nn_alpha_allowed
from agrocast.features.climatology import adaptive
from agrocast.features.dataset import feature_columns_for, make_test_row, training_data
from agrocast.forecast.asof import effective_cutoff
from agrocast.models import build_models
from agrocast.models.nn_kernel import PooledNN
import numpy as np
import pandas as pd

TERCILE_KEYS = ("below", "normal", "above")


def _r(x, nd):
    return round(float(x), nd)


def _confidence(rpss_val):
    from agrocast.backtest.metrics import CONFIDENCE_LOW, CONFIDENCE_MEDIUM

    if rpss_val is None or not np.isfinite(rpss_val):
        return {"rpss": None, "level": "unknown", "no_skill": False}
    level = "low" if rpss_val < CONFIDENCE_LOW else ("medium" if rpss_val < CONFIDENCE_MEDIUM else "high")
    return {"rpss": _r(rpss_val, 4), "level": level, "no_skill": bool(rpss_val <= 0.0)}


def _skill_lookup(smap, variable, target_month, lead):
    if smap is None:
        return None
    g = smap[(smap["variable"] == variable) & (smap["target_month"] == target_month) & (smap["lead"] == lead)]
    if len(g) == 0:
        return None
    return float(g["rpss"].iloc[0])


def nn_for(ctx, config, variable, first_year):
    a = load_alpha(config, ctx["mode"], variable)
    if a <= 0 or not nn_alpha_allowed(variable, config):
        return None
    key = (variable, int(first_year))
    cache = ctx.setdefault("nn_cache", {})
    if key not in cache:
        pool = feature_columns_for(ctx["pf"], variable, mode=ctx["mode"])
        nn = PooledNN().fit(ctx["pf"], ctx["stds"][variable], pool, ctx["mode"], int(first_year))
        cache[key] = (nn, a, pool) if nn.usable() else None
    return cache[key]


def prepare_artifacts(config, point, variables, mode, season_len):
    from agrocast.backtest.engine import load_skill_map

    pf = point.predictor_frame()
    monthly = point.monthly()
    blender = Blender.load(config.artifact_path(f"blender_{mode}.json")) or Blender.default(variables)
    pcalib = {}
    ccalib = {}
    for v in variables:
        pc = TercileCalibrator.load(config.artifact_path(f"calib_{mode}_{v}.json"))
        if pc is not None:
            pcalib[v] = pc
        cc = ConformalQuantileCalibrator.load(config.artifact_path(f"conformal_{mode}_{v}.json"))
        if cc is not None and cc.usable():
            ccalib[v] = cc
    rcalib = RegimeClimatology.load(config.artifact_path(f"regimeclim_{mode}.json"))
    mz = {}
    terc_map = {}
    for v, spec in SPECS.get(mode, {}).items():
        for k in spec["mems"]:
            mz["z" + ("t" if v == "t2m" else "p") + str(k)] = memory_z(monthly, v, k)
        for vx, k in spec.get("xmems", ()):
            mz["z" + ("t" if vx == "t2m" else "p") + str(k)] = memory_z(monthly, vx, k)
        if "h1" in spec["feats"]:
            terc_map[v] = tercile_grid(point, v)
    smap = load_skill_map(config, mode)
    sctx_map = {}
    try:
        from agrocast.blend.shrink import ShrinkContext, year_skills
        from agrocast.skill.ledger import load_ledger

        led_, _ = load_ledger(config, mode)
        if led_ is not None and not led_.empty:
            for v in variables:
                g = led_[led_["variable"] == v]
                if len(g) < 30:
                    continue
                sctx_map[v] = ShrinkContext(
                    mode,
                    point,
                    v,
                    year_skills(g[["p0", "p1", "p2"]].to_numpy(float), g["obs_tercile"].to_numpy(int), g["year"].to_numpy(int)),
                )
    except Exception:
        sctx_map = {}
    ospr_data = {}
    if config.ospr_enabled:
        try:
            _spr = point.ocean_spread(lead=3)
            if _spr is not None and len(_spr) >= 20:
                ospr_data = {"series": _spr, "min": float(_spr.min()), "max": float(_spr.max())}
        except Exception:
            ospr_data = {}
    calib = None
    try:
        from agrocast.ingest.stations import calibration_for_point

        calib = calibration_for_point(config, point.lat, point.lon, monthly)
    except Exception:
        calib = None
    apply_calib = None if point.fixed_calibration_active else calib
    if mode == "seasonal":
        stds = {v: point.seasonal_std(v, season_len) for v in variables}
        raws = {v: point.seasonal_raw(v, season_len) for v in variables}
    else:
        stds = {v: point.standardized(v) for v in variables}
        raws = {v: monthly[v] for v in variables}
    return {
        "point": point,
        "pf": pf,
        "monthly": monthly,
        "mode": mode,
        "season_len": int(season_len),
        "variables": tuple(variables),
        "blender": blender,
        "pcalib": pcalib,
        "ccalib": ccalib,
        "rcalib": rcalib,
        "mz": mz,
        "terc_map": terc_map,
        "smap": smap,
        "sctx_map": sctx_map,
        "ospr": ospr_data,
        "apply_calib": apply_calib,
        "calib": calib,
        "stds": stds,
        "raws": raws,
        "nn_cache": {},
    }


def predict_target(config, ctx, variable, tgt, sm, lead, issue, first_year=None, extra=False):
    pf = ctx["pf"]
    std = ctx["stds"][variable]
    series_raw = ctx["raws"][variable]
    mode = ctx["mode"]
    span = ctx["season_len"] if mode == "seasonal" else 1
    blender = ctx["blender"]
    smap = ctx["smap"]
    calib = ctx["apply_calib"]
    pcal = ctx["pcalib"].get(variable)
    ccal = ctx["ccalib"].get(variable)
    rcal = ctx["rcalib"]
    mz = ctx["mz"]
    terc_map = ctx["terc_map"]
    sctx = ctx["sctx_map"].get(variable)
    ospr = ctx["ospr"] if variable == "tp" else None
    mon = ctx["monthly"][variable]
    nstack = nn_for(ctx, config, variable, first_year) if first_year is not None else None
    a = adaptive(series_raw, tgt.year, tgt.month, config.clim_window, config.clim_half_life)
    if a is None:
        return None
    cutoff = effective_cutoff(issue, getattr(config, "publication_delay_days", 0))
    if cutoff not in pf.index:
        return None
    mu, sd = a["mu"], a["sd"]
    e1 = (a["q"][0.33] - mu) / sd
    e2 = (a["q"][0.67] - mu) / sd
    use_cols = feature_columns_for(pf, variable, mode=mode)
    X, yv, meta = training_data(pf, std, variable, sm, lead, until=cutoff, use_cols=use_cols, span=span)
    if len(X) < 18:
        return None
    w = exp_weights(len(X), config.clim_half_life)
    x_test = make_test_row(pf, cutoff, lead, tgt, use_cols=use_cols)
    preds = {}
    n_by_model = {}
    analogs = []
    for m in build_models(config, variable=variable, mode=mode):
        try:
            Xf, yf, metaf, wf = X, yv, meta, w
            if mode == "seasonal" and getattr(m, "season_months", None) and len(m.season_months) > 1:
                Xparts, yparts, mparts = [], [], []
                for s2 in m.season_months:
                    Xs, ys, ms = training_data(pf, std, variable, s2, lead, until=cutoff, use_cols=use_cols, span=span)
                    if len(Xs):
                        Xparts.append(Xs)
                        yparts.append(ys)
                        mparts.append(ms)
                if Xparts:
                    Xf = pd.concat(Xparts, ignore_index=True)
                    yf = np.concatenate(yparts)
                    metaf = pd.concat(mparts, ignore_index=True)
                    wf = exp_weights(len(Xf), config.clim_half_life)
            m.fit(Xf, yf, w=wf, edges=metaf[["e1", "e2"]].to_numpy(), years=metaf["year"].to_numpy())
            p, q = m.predict(x_test, e1, e2)
        except Exception:
            continue
        n_by_model[m.name] = int(len(Xf))
        preds[m.name] = (p, q)
        if m.name == "analog":
            analogs = m.analogs(x_test, k=8)
    if not preds:
        return None
    P, Q = blender.combine(variable, tgt.month, preds)
    if nstack is not None:
        nn, a, pool = nstack
        Pn = nn.probs_for(pf, cutoff, pool, tgt.month, lead)
        if Pn is not None and a > 0:
            P = (1.0 - a) * P + a * Pn
            P = P / P.sum()
    P_unc = P
    if pcal is not None and pcal.usable():
        P = pcal.transform(P.reshape(1, -1))[0]
    if mon is not None:
        from agrocast.blend import regime_guard

        if regime_guard.shifted(mon, std, variable, mode, cutoff, tgt if mode == "seasonal" else None, config=config):
            P = P_unc
    if rcal is not None:
        group = season_of(tgt.month) if mode == "seasonal" else f"m{int(tgt.month)}"
        if cutoff in pf.index:
            iprev = cutoff - 3
            x = rcal.feature_vector(
                variable,
                pf.loc[cutoff],
                cutoff,
                mz,
                pf.loc[iprev] if iprev in pf.index else None,
                (terc_map or {}).get(variable),
            )
            P = rcal.transform(P, variable, group, tgt.year, x)
    if sctx is not None:
        P = sctx.apply(P, tgt.month, tgt.year)
    if ccal is not None and ccal.usable():
        Q = ccal.transform(Q, variable, lead)
    if ospr is not None and "series" in ospr:
        _s = ospr["series"]
        if cutoff in _s.index:
            _sv = float(_s.loc[cutoff])
            if np.isfinite(_sv):
                _u = min(1.0, max(0.0, (_sv - ospr["min"]) / max(ospr["max"] - ospr["min"], 1e-9)))
                _w = config.ospr_weight * _u
                if _w > 0:
                    P = (1.0 - _w) * P + _w * np.array([1.0 / 3.0, 1.0 / 3.0, 1.0 / 3.0])
                    P = P / P.sum()
                    Q = (1.0 - _w) * np.asarray(Q, float)
    phys = mu + sd * Q
    bias, ratio = 0.0, 1.0
    if calib is not None:
        if variable == "t2m" and calib.get("t2m_bias") is not None:
            bias = float(calib["t2m_bias"])
        if variable == "tp" and calib.get("tp_ratio") is not None:
            ratio = float(calib["tp_ratio"])
    mu = mu * ratio + bias
    phys = phys * ratio + bias
    unit = "c" if variable == "t2m" else "mm"
    block = {
        "tercile_probs": {k: _r(P[i], 3) for i, k in enumerate(TERCILE_KEYS)},
        f"quantiles_{unit}": {"p10": _r(phys[0], 1), "p50": _r(phys[1], 1), "p90": _r(phys[2], 1)},
        f"normal_{unit}": _r(mu, 1),
        "model_probs": {name: [_r(pv[0][i], 3) for i in range(3)] for name, pv in preds.items()},
        "confidence": _confidence(_skill_lookup(smap, variable, tgt.month, lead)),
        "analog_years": [
            {"year": int(a_["year"]), "value": _r(mu + sd * a_["z"], 1), "weight": _r(a_["weight"], 3)}
            for a_ in analogs
        ],
        "_calc": {
            "mu": float(mu),
            "sd": float(sd),
            "e1": float(e1),
            "e2": float(e2),
            "P": [float(x) for x in P],
            "qz": [float(x) for x in Q],
        },
    }
    if variable == "t2m":
        block["anomaly_c"] = _r(sd * Q[1], 1)
    else:
        block["percent_of_normal"] = _r(100.0 * phys[1] / max(mu, 1e-6), 0)
    if extra:
        return block, phys, preds, n_by_model, cutoff
    return block, phys


