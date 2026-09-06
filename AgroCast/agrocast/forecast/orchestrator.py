import datetime as dt
from pathlib import Path

import numpy as np
from agrocast.core.errors import IssueFreshnessError
import pandas as pd

from agrocast.core.mathutils import exp_weights
from agrocast.core.timeutils import now_period
from agrocast.features.dataset import PointDataset, training_data, make_test_row, feature_columns_for
from agrocast.features.climatology import adaptive
from agrocast.models import build_models
from agrocast.forecast.asof import effective_cutoff, release_context, validate_context
from agrocast.forecast import pipeline
from agrocast.blend.blender import Blender
from agrocast.backtest.engine import load_skill_map, blender_name, skill_name
from agrocast.blend.calibration import TercileCalibrator
from agrocast.blend.blender import season_of
from agrocast.blend.regime_clim import RegimeClimatology, SPECS, memory_z, tercile_grid
from agrocast.models.nn_kernel import PooledNN
from agrocast.ingest.registry import Registry
from agrocast.agro.generator import WeatherGenerator
from agrocast.agro.indices import daily_indices, ensemble_indices, spi_block
from agrocast.blend.conformal import ConformalQuantileCalibrator
from agrocast.models.builder import MODEL_NAMES

TERCILE_KEYS = ["below", "normal", "above"]


def _r(x, nd=2):
    return round(float(x), nd)


def _what_to_do(agro):

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
    fr = (agro.get("insight") or {}).get("frost") or {}
    fc = fr.get("crop") or {}
    if fc.get("safe_date"):
        cards.append(
            {
                "action": f"Сев {fc.get('name') or 'сорта'} — не раньше ~{fc['safe_date']}",
                "reason": fc.get("verdict", ""),
                "level": "high" if (fc.get("danger_at_sow_from") or 0) > 0.10 else "mid",
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


def _sat_crop(v):
    if not v or not v.get("gdd"):
        return None
    return {
        "name": v["name"],
        "need": int(v["gdd"]),
        "note": f"сорт из справочника: ФАО {v.get('fao') or '—'}, САТ {int(v['gdd'])}° (параметры заполнены пользователем)",
    }


def align_issue(start, index):
    explicit = start is not None
    if start is None or not str(start).strip():
        start = now_period() + 1
    else:
        start = pd.Period(str(start), "M") if not isinstance(start, pd.Period) else start
    issue = start - 1
    if issue not in index:
        if explicit:
            raise IssueFreshnessError("requested month is newer than the last complete predictor row")
        issue = index[-1]
        start = issue + 1
    return start, issue


def forecast_point(config, lat, lon, start=None, horizon=3, variables=("t2m", "tp"), save=True, point=None, mode="monthly", season_len=3, variety=None):
    store = config.zarr_store()
    point = point or PointDataset(config, lat, lon, store)
    pf = point.predictor_frame()
    monthly = point.monthly()
    vcrop = None
    all_crops = []

    from agrocast.crops.db import CropDB

    _vdroot = config.runtime_dir or config.data_dir
    try:
        _cdb = CropDB(Path(_vdroot) / "crops.db", str(config.source_artifact("crop_seed.json")))
        all_crops = _cdb.all()
        if variety:
            vcrop = _cdb.get(variety)
    except Exception:
        vcrop = None
    horizon = int(min(max(int(horizon), 1), config.horizon_max))
    start, issue = align_issue(start, pf.index)
    obs_cutoff = effective_cutoff(issue, getattr(config, "publication_delay_days", 0))
    as_of = release_context(config, issue, observation_cutoff=obs_cutoff)
    validate_context(as_of)
    ctx = pipeline.prepare_artifacts(config, point, variables, mode, season_len)
    out_items = []
    phys_store = {}
    if mode == "seasonal":
        n_seas = max(1, (horizon + season_len - 1) // season_len)
        targets = [(start + season_len * k, k + 1) for k in range(n_seas)]
    else:
        targets = [(start + i, i + 1) for i in range(horizon)]
    first_year = min(t[0].year for t in targets)
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
            res = pipeline.predict_target(config, ctx, v, tgt, sm, lead, issue, first_year=first_year)
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
                    agro["insight"] = season_insight(ens, float(lat), monthly, swvl, sat_crop=_sat_crop(vcrop), sat_crops=all_crops)
                    from agrocast.agro.frost import frost_block

                    agro["insight"]["frost"] = frost_block(ens, vcrop)
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
                            skill = pd.read_parquet(config.artifact_path(skill_name(mode)))
                            recs = pd.read_parquet(config.artifact_path(records_name(mode)))
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
                    from agrocast.market.source import corn_price

                    try:
                        mprice = corn_price(config.runtime_dir or config.data_dir, config.bundle_dir or config.data_dir, timeout=10)
                    except Exception:
                        mprice = None
                    agro["econ"] = econ_block(
                        agro.get("phenology") or {},
                        drought_p,
                        heat_p,
                        price=mprice,
                        yield_t_ha=vcrop.get("yield_t_ha") if vcrop else None,
                        variety_name=vcrop.get("name") if vcrop else None,
                        water=(agro.get("insight") or {}).get("water"),
                    )
                except Exception:
                    pass

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
        "issue_data_through": str(obs_cutoff),
        "as_of": as_of,
        "variables": list(variables),
        "models": [m for m in MODEL_NAMES],
        "station_calibration": ctx["calib"],
        "targets_station_calibrated": bool(point.fixed_calibration_active),
        "weights": {v: {s: {k: _r(w, 4) for k, w in dd.items()} for s, dd in d.items()} for v, d in ctx["blender"].weights.items()},
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
