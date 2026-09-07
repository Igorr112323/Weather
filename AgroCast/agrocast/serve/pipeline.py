import hashlib
import traceback
import time
from uuid import UUID, uuid4
from pathlib import Path

import numpy as np
import pandas as pd

from agrocast.core.config import Config, Region
from agrocast.core.contracts import Coordinates
from agrocast.core.jsoncodec import canonical_json
from agrocast.core.settings import RuntimeSettings, ConfigurationError
from agrocast.store.atomic import write_json
from agrocast.ingest.openobs import fetch_cpc_daily, fetch_soil

HINDCAST_YEARS = list(range(2004, 2025))


def point_box(lat, lon):
    if lon < -20.0:
        lon += 360.0
    lo = max(0.0, round(lon - 1.8, 2))
    return Region(
        lat_min=max(-89.0, round(lat - 1.2, 2)),
        lat_max=min(89.0, round(lat + 1.2, 2)),
        lon_min=lo,
        lon_max=min(359.9, round(lon + 1.8, 2)),
    )


def world_config(world_dir=None, data_root=None, settings=None):
    settings = (settings or RuntimeSettings.from_environment()).with_paths(world_dir, data_root)
    return settings.compute_config()


def point_config(world_dir, data_root, lat, lon, config_snapshot=None):
    coordinates = Coordinates(lat=lat, lon=lon)
    lat, lon = coordinates.lat, coordinates.lon
    wc = Config.from_dict(config_snapshot) if config_snapshot is not None else world_config(world_dir, data_root)
    if config_snapshot is not None and (Path(wc.bundle_dir) != Path(world_dir).resolve() or Path(wc.runtime_dir) != Path(data_root).resolve()):
        raise ConfigurationError("Worker paths do not match the captured configuration")
    r = wc.region
    box = point_box(lat, lon)
    if box.lat_min >= r.lat_min and box.lat_max <= r.lat_max and box.lon_min >= r.lon_min and box.lon_max <= r.lon_max:
        return wc, wc.data_dir
    key = hashlib.sha256(canonical_json(coordinates.model_dump()).encode("utf-8")).hexdigest()
    values = wc.to_dict()
    values.update(data_dir=str(Path(data_root) / "compute" / "points" / key), region=box.__dict__, use_bundle_models=False)
    cfg = Config.from_dict(values)
    cfg.save()
    return cfg, cfg.data_dir


def _fit_artifacts(cfg, log):
    from agrocast.backtest.engine import run_backtest
    from agrocast.blend.blender import Blender, blended_records, attach_obs, season_of
    from agrocast.blend.calibration import gated_calibrator
    from agrocast.features.dataset import PointDataset

    for mode, leads in [("seasonal", [1]), ("monthly", list(range(1, 7)))]:
        log(f"обучаю модели: режим {mode}")
        rec = run_backtest(
            cfg,
            variables=("t2m", "tp"),
            start_months=list(range(1, 13)),
            leads=leads,
            years=range(2004, 2025),
            mode=mode,
            season_len=3,
            half_life_years=5.0,
        )
        pieces = []
        for y in sorted(rec.year.unique()):
            b = Blender(half_life_years=5.0).fit(rec[rec.year != y])
            br = blended_records(rec[rec.year == y], b.weights)
            pieces.append(br)
        blend = pd.concat(pieces, ignore_index=True)
        blend = attach_obs(blend, rec)
        blend["season"] = blend["target_month"].map(season_of)
        from agrocast.blend.conformal import ConformalQuantileCalibrator
        from agrocast.blend.regime_clim import RegimeClimatology, SPECS
        from agrocast.blend.nn_stack import select_alpha, save_alpha, nn_map, mix, row_keys

        _pt = PointDataset(cfg, cfg.region.lat_min + 0.5, cfg.region.lon_min + 0.5, cfg.zarr_store())
        alphas = {}
        nnmaps = {}
        for v in ("t2m", "tp"):
            sub = blend[blend.variable == v]
            nnmap = nn_map(_pt, v, mode, sorted(sub["year"].unique()))
            nnmaps[v] = nnmap
            a = select_alpha(_pt, v, mode, blend, nnmap=nnmap)
            alphas[v] = a
            save_alpha(cfg, mode, v, a)
            log(f"нейроядро {mode} {v}: alpha={a}")
        for v in ("t2m", "tp"):
            sub = blend[blend.variable == v]
            P = sub[["p0", "p1", "p2"]].to_numpy(float)
            if alphas[v] > 0:
                P = mix(P, row_keys(sub), nnmaps[v], alphas[v])
            cal = gated_calibrator(P, sub.obs_tercile.to_numpy(), years=sub["year"].to_numpy())
            cal_path = cfg.artifact_dir / f"calib_{mode}_{v}.json"
            cal.save(cal_path)
            ccal = ConformalQuantileCalibrator().fit(sub)
            ccal.save(cfg.artifact_dir / f"conformal_{mode}_{v}.json")
            log(f"калибровка {mode} {v}: {cal.n} записей")
        if SPECS.get(mode):
            rc = RegimeClimatology.fit_history(mode, _pt)
            rc.save_live(cfg.artifact_dir / f"regimeclim_{mode}.json")
            log(f"режимная климатология {mode}: группы {sorted(set(g for _, g in rc.history))}")


def ensure_point(cfg, world_dir, log):
    import os

    marker = Path(cfg.data_dir) / "ready.json"
    if marker.exists():
        log("данные точки уже готовы")
        return
    bundle_ready = Path(cfg.bundle_dir) / "ready.json" if cfg.bundle_dir else None
    if cfg.use_bundle_models and bundle_ready is not None and bundle_ready.exists():
        log("читаю модели из read-only bundle; это не проверка научного допуска")
        return
    wcfg = world_config(world_dir, cfg.runtime_dir or cfg.data_dir)
    inside = (
        cfg.region.lat_min >= wcfg.region.lat_min
        and cfg.region.lat_max <= wcfg.region.lat_max
        and cfg.region.lon_min >= wcfg.region.lon_min
        and cfg.region.lon_max <= wcfg.region.lon_max
    )
    if not inside:
        if os.environ.get("AGROCAST_DESKTOP", "") == "1":
            raise RuntimeError(
                "точка (%.2f, %.2f) вне набора данных программы; расчёт невозможен без интернета"
                % (cfg.region.lat_min, cfg.region.lon_min)
            )
        log("скачиваю суточные наблюдения CPC для точки (1979–2026, обычно 2–6 минут)")
        fetch_cpc_daily(cfg, workers=4)
        log("суточные данные готовы")
        log("скачиваю почвенную влагу NCEP")
        fetch_soil(cfg)
        log("почва готова")
    _fit_artifacts(cfg, log)
    write_json(marker, {"ok": True})
    log("точка готова")


def run_hindcast(cfg, lat, lon, start, mode, horizon, log):
    from agrocast.skill.ledger import load_ledger

    start = pd.Period(str(start), "M")
    horizon = int(horizon)
    if mode == "seasonal":
        targets = [start + 3 * k for k in range(max(1, (horizon + 2) // 3))]
    else:
        targets = [start + k for k in range(horizon)]
    if targets[-1] > pd.Period("2024-12", "M"):
        raise ValueError("горизонт уходит за пределы честной проверки (до 2024-12): выберите более раннюю дату или меньший период")
    if targets[0] < pd.Period("2004-01", "M"):
        raise ValueError("проверка на истории доступна с 2004 года")
    log(f"проверка на истории: старт {start}, режим {mode}, целей {len(targets)}")
    led, _ = load_ledger(cfg, mode)
    if led is None or led.empty:
        rec_path = cfg.artifact_path(f"backtest_records_{mode}.parquet")
        if not rec_path.exists():
            raise ValueError("нет walk-forward ledger: сначала пересоберите бэктест (agrocast backtest) для режима " + mode)
        from agrocast.skill.ledger import build_ledger

        log("ledger не найден в артефактах: пересчёт walk-forward folds по записям бэктеста")
        led = build_ledger(pd.read_parquet(rec_path), mode=mode, config=cfg)
    if led is None or led.empty:
        raise ValueError("для этой даты нет записей проверки")
    from agrocast.features.dataset import PointDataset

    pt = PointDataset(cfg, lat, lon, cfg.zarr_store())
    stds = {v: (pt.seasonal_std(v, 3) if mode == "seasonal" else pt.standardized(v)) for v in ("t2m", "tp")}
    items = []
    for t in targets:
        block = {"year": int(t.year), "target_month": int(t.month)}
        for v in ("t2m", "tp"):
            g = led[(led["variable"] == v) & (led["year"] == int(t.year)) & (led["target_month"] == int(t.month))]
            if g.empty:
                continue
            std = stds[v]
            if t not in std.index:
                continue
            r = g.iloc[0]
            mu = float(std.loc[t, "mu"])
            sd = float(std.loc[t, "sd"])
            z = float(std.loc[t, "z"])
            P = [float(r["p0"]), float(r["p1"]), float(r["p2"])]
            obs = int(r["obs_tercile"])
            dom = int(np.argmax(P))
            block[v] = {
                "probs": [round(float(x), 3) for x in P],
                "dominant": dom,
                "obs": obs,
                "hit": bool(dom == obs),
                "fact": round(mu + sd * z, 1),
                "p50": round(mu + sd * float(r["q50"]), 1),
                "p10": round(mu + sd * float(r["q10"]), 1),
                "p90": round(mu + sd * float(r["q90"]), 1),
                "norm": round(mu, 1),
                "unit": "c" if v == "t2m" else "mm",
            }
        if "t2m" in block or "tp" in block:
            items.append(block)
    if not items:
        raise ValueError("для этой даты нет записей проверки")
    summary = {}
    for v in ("t2m", "tp"):
        hits = [it[v]["hit"] for it in items if v in it]
        summary[v] = {"hits": int(sum(hits)), "total": len(hits)}
    log(f"итог: температура {summary['t2m']['hits']}/{summary['t2m']['total']}, осадки {summary['tp']['hits']}/{summary['tp']['total']}")
    return {"kind": "hindcast", "start": str(start), "mode": mode, "lat": float(lat), "lon": float(lon), "items": items, "summary": summary}


class Job:
    def __init__(self, job_id, params):
        try:
            self.id = str(UUID(str(job_id)))
        except ValueError:
            self.id = str(uuid4())
        self.legacy_id = str(job_id)
        self.config_snapshot = None
        self.created_at = int(time.time())
        self.params = params
        self.status = "running"
        self.log = []
        self.result = None
        self.error = None

    def add(self, line):
        self.log.append(line)


def run_job(job, world_dir, data_root):
    settings = RuntimeSettings.from_environment().with_paths(world_dir, data_root)
    settings.prepare_state()
    if job.config_snapshot is None:
        job.config_snapshot = settings.compute_config().to_dict()
    snapshot_path = settings.state_dir / "offline-jobs" / (job.id + ".json")

    def persist():
        write_json(snapshot_path, {"id": job.id, "legacy_id": job.legacy_id, "params": job.params, "status": job.status,
            "result": job.result, "error": job.error, "log": job.log, "created_at": job.created_at,
            "updated_at": int(time.time()), "config": job.config_snapshot, "ownership": "unassigned_offline"})

    persist()
    try:
        from agrocast.forecast.orchestrator import forecast_point

        lat = float(job.params["lat"])
        lon = float(job.params["lon"])
        kind = job.params.get("kind", "forecast")
        cfg, _ = point_config(world_dir, data_root, lat, lon, job.config_snapshot)
        job.add(f"точка {lat:.2f}°N {lon:.2f}°E")
        ensure_point(cfg, world_dir, job.add)
        if kind == "hindcast":
            job.result = run_hindcast(
                cfg,
                lat,
                lon,
                job.params.get("start"),
                job.params.get("mode", "seasonal"),
                int(job.params.get("horizon", 3)),
                job.add,
            )
        else:
            job.add("считаю прогноз")
            job.result = forecast_point(
                cfg,
                lat,
                lon,
                start=str(job.params["start"]),
                horizon=int(job.params["horizon"]),
                mode=job.params.get("mode", "seasonal"),
                season_len=int(job.params.get("season_len", 3)),
                save=True,
                variety=job.params.get("variety", ""),
            )
        job.status = "done"
        job.add("готово")
    except Exception as exc:
        job.status = "error"
        job.error = f"{type(exc).__name__}: {exc}"
        job.add("ошибка: " + job.error)
        traceback.print_exc()
    finally:
        persist()



