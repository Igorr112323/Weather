import pandas as pd

from agrocast.core.geo import region_center
from agrocast.ingest.registry import Registry
from agrocast.features.dataset import PointDataset
from agrocast.forecast import pipeline
from agrocast.forecast.asof import (EVAL_PROSPECTIVE, EVAL_REPLAY_REVISED, effective_cutoff, target_window_end,
                              vintages_available)
from agrocast.blend.blender import Blender, blended_records
from agrocast.backtest.metrics import rpss


def records_name(mode):
    return f"backtest_records_{mode}.parquet"


def blender_name(mode):
    return f"blender_{mode}.json"


def skill_name(mode):
    return f"skill_map_{mode}.parquet"


def pipeline_table_name(mode):
    return f"backtest_pipeline_{mode}.parquet"


def run_backtest(config, variables=("t2m", "tp"), start_months=None, leads=None, years=None, lat=None, lon=None, point=None, mode="monthly", season_len=3, half_life_years=0.0, save_artifacts=True, publication_delay_days=None, return_pipeline=False):
    delay = int(config.publication_delay_days if publication_delay_days is None else publication_delay_days)
    store = config.zarr_store()
    reg = Registry(config.registry_path)
    if point is None:
        lat, lon = (lat, lon) if lat is not None and lon is not None else region_center(config.region)
        point = PointDataset(config, lat, lon, store)
    pf = point.predictor_frame()
    monthly = point.monthly()
    if mode == "seasonal":
        leads = [1]
    ctx = pipeline.prepare_artifacts(config, point, variables, mode, season_len)
    stds = ctx["stds"]
    last_p = monthly.index[-1]
    years = list(years) if years is not None else list(range(config.backtest_start, last_p.year + 1))
    start_months = list(start_months) if start_months is not None else list(range(1, 13))
    leads = list(leads) if leads is not None else list(range(1, config.horizon_max + 1))
    rows = []
    rows_final = []
    for y in years:
        for v in variables:
            std = stds[v]
            if len(std) < 40:
                continue
            for sm in start_months:
                start = pd.Period(f"{y}-{int(sm):02d}", "M")
                issue = start - 1
                if issue not in pf.index:
                    continue
                for lead in leads:
                    tgt = start + (lead - 1)
                    span = season_len if mode == "seasonal" else 1
                    tgt_end = target_window_end(tgt, span)
                    if tgt_end > last_p:
                        continue
                    cutoff = effective_cutoff(issue, delay)
                    if cutoff not in pf.index:
                        continue
                    res = pipeline.predict_target(config, ctx, v, tgt, sm, lead, issue, first_year=start.year, extra=True)
                    if res is None:
                        continue
                    block, _phys, preds, n_by_model, cutoff_used = res
                    if cutoff_used != cutoff:
                        continue
                    trow = std.loc[tgt]
                    obs_z = float(trow["z"])
                    obs_terc = int(0 if obs_z < float(trow["e1"]) else (1 if obs_z <= float(trow["e2"]) else 2))
                    for m_name, (mp, mq) in preds.items():
                        rows.append(
                            {
                                "variable": v,
                                "start_month": int(sm),
                                "lead": int(lead),
                                "target_month": int(tgt.month),
                                "year": int(y),
                                "model": m_name,
                                "issue": str(issue),
                                "observation_cutoff": str(cutoff),
                                "train_until": str(cutoff),
                                "train_n": int(n_by_model.get(m_name, 0)),
                                "target_start": str(tgt),
                                "target_end": str(tgt_end),
                                "p0": float(mp[0]),
                                "p1": float(mp[1]),
                                "p2": float(mp[2]),
                                "q10": float(mq[0]),
                                "q50": float(mq[1]),
                                "q90": float(mq[2]),
                                "obs_z": obs_z,
                                "obs_tercile": obs_terc,
                                "mu": float(trow["mu"]),
                                "sd": float(trow["sd"]),
                            }
                        )
                    calc = block["_calc"]
                    rows_final.append(
                        {
                            "variable": v,
                            "start_month": int(sm),
                            "lead": int(lead),
                            "target_month": int(tgt.month),
                            "year": int(y),
                            "issue": str(issue),
                            "observation_cutoff": str(cutoff),
                            "train_until": str(cutoff),
                            "target_start": str(tgt),
                            "target_end": str(tgt_end),
                            "p0": float(calc["P"][0]),
                            "p1": float(calc["P"][1]),
                            "p2": float(calc["P"][2]),
                            "q10": float(calc["qz"][0]),
                            "q50": float(calc["qz"][1]),
                            "q90": float(calc["qz"][2]),
                            "obs_z": obs_z,
                            "obs_tercile": obs_terc,
                            "mu": float(calc["mu"]),
                            "sd": float(calc["sd"]),
                        }
                    )
    records = pd.DataFrame(rows)
    if records.empty:
        reg.log_event("backtest", f"no records mode={mode}")
        return records
    vint = {i: vintages_available(config, i) for i in sorted(set(records["issue"]))}
    records["evaluation"] = [EVAL_PROSPECTIVE if vint[i] else EVAL_REPLAY_REVISED for i in records["issue"]]
    pipe = pd.DataFrame(rows_final) if rows_final else pd.DataFrame()
    if not pipe.empty:
        pipe["evaluation"] = [vint[i] and EVAL_PROSPECTIVE or EVAL_REPLAY_REVISED for i in pipe["issue"]]
    if save_artifacts and rows_final:
        pipe.to_parquet(config.artifact_dir / pipeline_table_name(mode))
    if save_artifacts:
        records.to_parquet(config.artifact_dir / records_name(mode))
        blender = Blender(half_life_years=half_life_years).fit(records)
        blender.save(config.artifact_dir / blender_name(mode))
        led = None
        try:
            from agrocast.skill.ledger import build_ledger, save_ledger

            led = build_ledger(records, mode=mode, config=config, half_life_years=half_life_years)
            s = save_ledger(led, config, mode)
            if s:
                reg.log_event("backtest", f"ledger mode={mode} n={s['overall']['n']} rpss={s['overall']['rpss']}")
        except Exception as exc:
            led = None
            reg.log_event("backtest", f"ledger failed mode={mode}: {exc}")
        if led is not None and not led.empty:
            br = led
            skill_source = "walk_forward"
        else:
            br = blended_records(records, blender.weights)
            skill_source = "in_sample_fallback"
        smap = []
        for (v, tm, ld), g in br.groupby(["variable", "target_month", "lead"]):
            obs = g["obs_tercile"].to_numpy(int)
            probs = g[["p0", "p1", "p2"]].to_numpy(float)
            smap.append({"variable": v, "target_month": int(tm), "lead": int(ld), "rpss": float(rpss(probs, obs)), "n": len(g), "skill_source": skill_source})
        pd.DataFrame(smap).to_parquet(config.artifact_dir / skill_name(mode))
    reg.log_event("backtest", f"mode={mode} rows={len(records)} years={years[0]}..{years[-1]}")
    if return_pipeline:
        return records, pipe
    return records


def load_records(config, mode="monthly"):
    p = config.artifact_path(records_name(mode))
    return pd.read_parquet(p) if p.exists() else None


def load_skill_map(config, mode="monthly"):
    p = config.artifact_path(skill_name(mode))
    return pd.read_parquet(p) if p.exists() else None


def skill_summary(records):
    out = []
    for (v, m), g in records.groupby(["variable", "model"]):
        obs = g["obs_tercile"].to_numpy(int)
        probs = g[["p0", "p1", "p2"]].to_numpy(float)
        out.append({"variable": v, "model": m, "rpss": rpss(probs, obs), "n": len(g)})
    return pd.DataFrame(out)
