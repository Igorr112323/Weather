import datetime as dt
import os
import numpy as np
import pandas as pd

from agrocast.core.config import Config
from agrocast.core.mathutils import exp_weights
from agrocast.core.timeutils import now_period
from agrocast.features.dataset import PointDataset, training_data, make_test_row, feature_columns_for
from agrocast.features.climatology import adaptive
from agrocast.models import build_models
from agrocast.blend.blender import Blender
from agrocast.backtest.engine import load_skill_map, blender_name, skill_name
from agrocast.blend.calibration import TercileCalibrator
from agrocast.blend.blender import season_of
from agrocast.blend.regime_clim import RegimeClimatology, SPECS, memory_z, tercile_grid
from agrocast.models.nn_kernel import PooledNN
from agrocast.ingest.registry import Registry
from agrocast.store.zarrstore import ZarrStore
from agrocast.agro.generator import WeatherGenerator
from agrocast.agro.indices import daily_indices, ensemble_indices, spi_block
from agrocast.blend.conformal import ConformalQuantileCalibrator
from agrocast.models.builder import MODEL_NAMES

TERCILE_KEYS = ["below", "normal", "above"]
W_OSPR = 0.20


def _r(x, nd=2):
    return round(float(x), nd)


def _what_to_do(agro):
    """Топ-3 простых действия для фермера: «что, почему, на сколько критично»."""
    cards = []
    for d in agro.get("decisions") or []:
        if d["verdict"] == "действовать":
            cards.append(
                {
                    "action": d["label"],
                    "reason": f"вероятность события {round(d['prob'] * 100)}% выше порога окупаемости {round(d['ratio'] * 100)}%",
                    "note": d.get("note"),
                    "level": "high",
                }
            )
        elif d["verdict"] == "на грани — решать вам":
            cards.append(
                {
                    "action": d["label"],
                    "reason": f"вероятность {round(d['prob'] * 100)}% около порога {round(d['ratio'] * 100)}%",
                    "note": d.get("note"),
                    "level": "mid",
                }
            )
    ins = agro.get("insight") or {}
    dr = ins.get("drought") or {}
    if dr.get("irrigation_hint_m3_ha"):
        cards.append(
            {
                "action": f"Запланировать полив ≈{dr['irrigation_hint_m3_ha']} м³/га",
                "reason": f"ожидаемый дефицит влаги {dr.get('deficit_mm', '?')} мм · риск засухи: {dr.get('risk_level', '?')}",
                "level": "high" if dr.get("risk_level") in ("высокий", "повышенный") else "mid",
            }
        )
    ph = agro.get("phenology") or {}
    for c in (ph.get("crops") or [])[:2]:
        for s in c.get("stages") or []:
            if s.get("prob", 0) >= 0.5 and s.get("action"):
                cards.append(
                    {
                        "action": s["action"],
                        "reason": f"{c.get('name', '')}: «{s.get('name', '')}» — вероятность события {round(s['prob'] * 100)}%",
                        "level": "high",
                    }
                )
    seen, out = set(), []
    for c in cards:
        if c["action"] in seen:
            continue
        seen.add(c["action"])
        out.append(c)
    order = {"high": 0, "mid": 1, "low": 2}
    out.sort(key=lambda c: order.get(c.get("level"), 3))
    return out[:3]


def _confidence(rpss_val):
    if rpss_val is None or not np.isfinite(rpss_val):
        return {"rpss": None, "level": "unknown"}
    level = "low" if rpss_val < 0.02 else ("medium" if rpss_val < 0.06 else "high")
    return {"rpss": _r(rpss_val, 4), "level": level}


def _skill_lookup(smap, variable, target_month, lead):
    if smap is None:
        return None
    g = smap[(smap["variable"] == variable) & (smap["target_month"] == target_month) & (smap["lead"] == lead)]
    if len(g) == 0:
        return None
    return float(g["rpss"].iloc[0])


def _fit_predict_target(config, pf, std, series_raw, variable, tgt, sm, lead, issue, blender, smap, calib=None, mode="monthly", pcal=None, nstack=None, ccal=None, rcal=None, mz=None, terc_map=None, sctx=None, ospr=None):
    a = adaptive(series_raw, tgt.year, tgt.month, config.clim_window, config.clim_half_life)
    if a is None:
        return None
    mu, sd = a["mu"], a["sd"]
    e1 = (a["q"][0.33] - mu) / sd
    e2 = (a["q"][0.67] - mu) / sd
    use_cols = feature_columns_for(pf, variable, mode=mode)
    X, yv, meta = training_data(pf, std, variable, sm, lead, until=issue, use_cols=use_cols)
    if len(X) < 18:
        return None
    w = exp_weights(len(X), config.clim_half_life)
    x_test = make_test_row(pf, issue, lead, tgt, use_cols=use_cols)
    preds = {}
    analogs = []
    for m in build_models(config):
        try:
            m.fit(X, yv, w=w, edges=meta[["e1", "e2"]].to_numpy(), years=meta["year"].to_numpy())
            p, q = m.predict(x_test, e1, e2)
        except Exception:
            continue
        preds[m.name] = (p, q)
        if m.name == "analog":
            analogs = m.analogs(x_test, k=8)
    if not preds:
        return None
    P, Q = blender.combine(variable, tgt.month, preds)
    if nstack is not None:
        nn, a, pool = nstack
        Pn = nn.probs_for(pf, issue, pool, tgt.month, lead)
        if Pn is not None and a > 0:
            P = (1.0 - a) * P + a * Pn
            P = P / P.sum()
    if pcal is not None and pcal.usable():
        P = pcal.transform(P.reshape(1, -1))[0]
    if rcal is not None:
        group = season_of(tgt.month) if mode == "seasonal" else f"m{int(tgt.month)}"
        if issue in pf.index:
            iprev = issue - 3
            x = rcal.feature_vector(
                variable,
                pf.loc[issue],
                issue,
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
        if issue in _s.index:
            _sv = float(_s.loc[issue])
            if np.isfinite(_sv):
                _u = min(1.0, max(0.0, (_sv - ospr["min"]) / max(ospr["max"] - ospr["min"], 1e-9)))
                _w = W_OSPR * _u
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
    return block, phys


def forecast_point(config, lat, lon, start=None, horizon=3, variables=("t2m", "tp"), save=True, point=None, mode="monthly", season_len=3):
    store = config.zarr_store()
    point = point or PointDataset(config, lat, lon, store)
    pf = point.predictor_frame()
    monthly = point.monthly()
    horizon = int(min(max(int(horizon), 1), config.horizon_max))
    explicit = start is not None
    if start is None:
        start = now_period() + 1
    else:
        start = pd.Period(str(start), "M") if not isinstance(start, pd.Period) else start
    issue = start - 1
    if issue not in pf.index:
        issue = pf.index[-1]
        if not explicit:
            start = issue + 1
    blender = Blender.load(config.artifact_dir / blender_name(mode)) or Blender.default(variables)
    pcalib = {}
    ccalib = {}
    for v in variables:
        pc = TercileCalibrator.load(config.artifact_dir / f"calib_{mode}_{v}.json")
        if pc is not None:
            pcalib[v] = pc
        cc = ConformalQuantileCalibrator.load(config.artifact_dir / f"conformal_{mode}_{v}.json")
        if cc is not None and cc.usable():
            ccalib[v] = cc
    rcalib = RegimeClimatology.load(config.artifact_dir / f"regimeclim_{mode}.json")
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
    if os.environ.get("AGROCAST_OSPR", "1") != "0":
        try:
            _spr = point.ocean_spread(lead=3)
            if _spr is not None and len(_spr) >= 20:
                ospr_data = {"series": _spr, "min": float(_spr.min()), "max": float(_spr.max())}
        except Exception:
            ospr_data = {}
    calib = None
    try:
        from agrocast.ingest.stations import calibration_for_point

        calib = calibration_for_point(config, lat, lon, monthly)
    except Exception:
        calib = None
    apply_calib = None if point.fixed_calibration_active else calib
    if mode == "seasonal":
        stds = {v: point.seasonal_std(v, season_len) for v in variables}
        raws = {v: point.seasonal_raw(v, season_len) for v in variables}
    else:
        stds = {v: point.standardized(v) for v in variables}
        raws = {v: monthly[v] for v in variables}
    out_items = []
    phys_store = {}
    if mode == "seasonal":
        n_seas = max(1, (horizon + season_len - 1) // season_len)
        targets = [(start + season_len * k, k + 1) for k in range(n_seas)]
    else:
        targets = [(start + i, i + 1) for i in range(horizon)]
    nnstack = {}
    for v in variables:
        sp = config.artifact_dir / f"stack_{mode}_{v}.json"
        if sp.exists():
            import json as _json

            a = float(_json.loads(sp.read_text()).get("alpha", 0.0))
            if a > 0:
                pool = feature_columns_for(pf, v, mode=mode)
                first_tgt_year = min(t[0].year for t in targets)
                nn = PooledNN().fit(pf, stds[v], pool, mode, first_tgt_year)
                if nn.usable():
                    nnstack[v] = (nn, a, pool)
    for tgt, display_lead in targets:
        lead = display_lead if mode == "monthly" else 1
        entry = {
            "lead": int(display_lead),
            "year": int(tgt.year),
            "month": int(tgt.month),
        }
        if mode == "seasonal":
            span = [tgt + i for i in range(season_len)]
            entry["months"] = [str(p) for p in span]
        got_any = False
        sm = start.month if mode == "monthly" else tgt.month
        for v in variables:
            pcal = pcalib.get(v)
            nstack = nnstack.get(v)
            ccal = ccalib.get(v)
            res = _fit_predict_target(config, pf, stds[v], raws[v], v, tgt, sm, lead, issue, blender, smap, apply_calib, mode=mode, pcal=pcal, nstack=nstack, ccal=ccal, rcal=rcalib, mz=mz, terc_map=terc_map, sctx=sctx_map.get(v), ospr=(ospr_data if v == "tp" else None))
            if res is None:
                continue
            block, phys = res
            if v == "tp" and mode == "seasonal":
                per_month = phys / float(season_len)
            elif v == "t2m" and mode == "seasonal":
                per_month = phys
            else:
                per_month = phys
            entry[v] = block
            span_periods = [tgt + i for i in range(season_len)] if mode == "seasonal" else [tgt]
            for p in span_periods:
                phys_store.setdefault((p.year, p.month), {})[v] = per_month
            got_any = True
        if got_any:
            out_items.append(entry)
    agro = None
    if phys_store:
        targets_g = {}
        for k, d in phys_store.items():
            if "t2m" in d and "tp" in d:
                targets_g[k] = {"t_q": d["t2m"].tolist(), "tp_q": d["tp"].tolist()}
        if targets_g:
            wg = WeatherGenerator(config.random_state).fit(point.daily())
            first = min(targets_g.keys(), key=lambda k: (k[0], k[1]))
            n_months = len(targets_g)
            ens = wg.generate(pd.Period(f"{first[0]}-{first[1]:02d}", "M"), n_months, targets_g, n=config.n_ensemble)
            if ens:
                idx = ensemble_indices([daily_indices(df) for df in ens])
                spi_targets = {k: {"p10": v["tp_q"][0], "p50": v["tp_q"][1], "p90": v["tp_q"][2]} for k, v in targets_g.items()}
                spi = spi_block(monthly["tp"], spi_targets)
                agro = {"season_indices": idx, "spi": spi, "n_ensemble": len(ens)}
                try:
                    from agrocast.agro.insight import season_insight, analogs_facts, passport
                    from agrocast.backtest.engine import records_name

                    soil = point.soil_monthly()
                    swvl = None
                    if soil is not None and "swvl" in list(getattr(soil, "columns", [])):
                        s = soil["swvl"].dropna()
                        if len(s):
                            swvl = float(s.iloc[-1])
                    agro["insight"] = season_insight(ens, float(lat), monthly, swvl)
                    from agrocast.agro.phenology import phenology_block
                    from agrocast.agro.drivers import top_drivers
                    from agrocast.agro.decide import decision_table

                    agro["phenology"] = phenology_block(ens)
                    it0 = out_items[0] if out_items else None
                    ay = ((it0.get("t2m") or {}).get("analog_years") or []) if it0 else []
                    s_months = [int(str(m).split("-")[1]) for m in (it0.get("months") or [])] if it0 else []
                    if ay and s_months:
                        agro["analogs_facts"] = analogs_facts(ay, monthly, s_months, it0.get("year"))
                        try:
                            skill = pd.read_parquet(config.artifact_dir / skill_name(mode))
                            recs = pd.read_parquet(config.artifact_dir / records_name(mode))
                            agro["passport"] = passport(skill, recs, "t2m", s_months, mode)
                        except Exception:
                            pass
                    it0_t = (it0.get("t2m") or {}).get("tercile_probs") or {}
                    it0_p = (it0.get("tp") or {}).get("tercile_probs") or {}
                    if s_months:
                        try:
                            agro["drivers"] = {
                                "t2m": top_drivers(pf, stds["t2m"], s_months[0], "t2m", 3),
                                "tp": top_drivers(pf, stds["tp"], s_months[0], "tp", 2),
                            }
                        except Exception:
                            pass
                    ph = agro.get("phenology") or {}
                    drought_p = ph.get("drought_p")
                    if drought_p is None:
                        drought_p = float(it0_p.get("below", 1.0 / 3.0))
                    warm = bool(set(s_months) & {5, 6, 7, 8})
                    heat_p = float(it0_t.get("above", 1.0 / 3.0)) if warm else None
                    agro["decisions"] = decision_table(drought_p, heat_p)
                except Exception:
                    pass
                try:
                    from agrocast.agro.prices import econ_block

                    agro["econ"] = econ_block(agro.get("phenology") or {}, drought_p, heat_p)
                except Exception:
                    pass
    # Реестр доверия: публичный счёт навыка (backtest-лет + живые выпуски)
    try:
        from agrocast.skill.ledger import live_summary, load_ledger

        _, tl = load_ledger(config, mode)
        if tl:
            lv = live_summary(config)
            if lv:
                tl["live"] = lv
            if agro is None:
                agro = {}
            agro["trust_ledger"] = tl
    except Exception:
        pass
    if agro:
        try:
            agro["what_to_do"] = _what_to_do(agro)
        except Exception:
            pass
    payload = {
        "engine": "agrocast",
        "mode": mode,
        "season_len": season_len if mode == "seasonal" else None,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "lat": float(lat),
        "lon": float(lon),
        "grid_point": {"lat": point.grid_lat, "lon": point.grid_lon},
        "start": str(start),
        "horizon": horizon,
        "issue_data_through": str(issue),
        "variables": list(variables),
        "models": [m for m in MODEL_NAMES],
        "station_calibration": calib,
        "targets_station_calibrated": bool(point.fixed_calibration_active),
        "weights": {v: {s: {k: _r(w, 4) for k, w in dd.items()} for s, dd in d.items()} for v, d in blender.weights.items()},
        "months" if mode == "monthly" else "seasons": out_items,
        "agro": agro,
    }
    try:
        from agrocast.agro.advisor import season_advice

        items = payload.get("seasons") or payload.get("months") or []
        idx = (agro or {}).get("season_indices", {}) if agro else {}
        for i, entry in enumerate(items):
            gtk_p50 = None
            if isinstance(idx, dict):
                g = idx.get("htk", idx.get("gtk"))
                if isinstance(g, dict):
                    gtk_p50 = g.get("p50")
                elif isinstance(g, (int, float)):
                    gtk_p50 = g
            spi_p50 = None
            sp = (agro or {}).get("spi", {}) if agro else {}
            months_of = entry.get("months") or [f"{entry['year']}-{entry['month']:02d}"]
            key = months_of[0][:7] if months_of else None
            if isinstance(sp, dict) and key in sp:
                spi_p50 = sp[key].get("p50")
            adv = season_advice(entry, gtk_p50=gtk_p50, spi_p50=spi_p50, tp_norm_mm=entry.get("tp", {}).get("normal_mm"))
            if adv is not None:
                entry["advice"] = adv
    except Exception:
        pass
    if save:
        reg = Registry(config.registry_path)
        reg.save_forecast(lat, lon, start.year, start.month, horizon, payload)
        # Живой реестр доверия: фиксируем выпуск с хэшем входов (подлинностно)
        try:
            from agrocast.skill.ledger import append_live

            if mode == "seasonal" and out_items:
                it0 = out_items[0]
                tgt = f"{it0['year']}-{it0['month']:02d}"
                for v in ("t2m", "tp"):
                    blk = it0.get(v) or {}
                    calc = blk.get("_calc") or {}
                    P, qz = calc.get("P"), calc.get("qz")
                    if P and qz:
                        append_live(config, str(issue), lat, lon, v, P, qz, target=tgt)
        except Exception:
            pass
    return payload
