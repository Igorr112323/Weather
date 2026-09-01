import json
import threading
import traceback
from pathlib import Path

import numpy as np
import pandas as pd

from agrocast.core.config import Config, Region
from agrocast.ingest.openobs import fetch_cpc_daily, fetch_soil

HINDCAST_YEARS = list(range(2008, 2025))


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


def world_config(world_dir):
    wc = Config.load(str(Path(world_dir) / "config.json"))
    wc.data_dir = str(Path(world_dir))
    if not wc.shared_zarr:
        wc.shared_zarr = str(Path(world_dir) / "zarr")
    return wc


def point_config(world_dir, data_root, lat, lon):
    wc = world_config(world_dir)
    r = wc.region
    box = point_box(lat, lon)
    if box.lat_min >= r.lat_min and box.lat_max <= r.lat_max and box.lon_min >= r.lon_min and box.lon_max <= r.lon_max:
        return wc, str(Path(world_dir))
    key = f"{lat:.2f}_{lon:.2f}".replace("-", "m")
    pdir = Path(data_root) / "points" / key
    cfg = Config(
        data_dir=str(pdir),
        region=Region(lat_min=box.lat_min, lat_max=box.lat_max, lon_min=box.lon_min, lon_max=box.lon_max),
        shared_zarr=str(Path(world_dir) / "zarr"),
    )
    pdir.mkdir(parents=True, exist_ok=True)
    cfg.save()
    return cfg, str(pdir)


def _fit_artifacts(cfg, log):
    from agrocast.backtest.engine import run_backtest
    from agrocast.blend.blender import Blender, blended_records
    from agrocast.blend.calibration import TercileCalibrator
    from agrocast.features.dataset import PointDataset, feature_columns_for
    from agrocast.models.nn_kernel import PooledNN
    from agrocast.backtest.metrics import rps_mean

    for mode, leads in [("seasonal", [1]), ("monthly", list(range(1, 7)))]:
        log(f"обучаю модели: режим {mode}")
        rec = run_backtest(
            cfg,
            variables=("t2m", "tp"),
            start_months=list(range(1, 13)),
            leads=leads,
            years=range(2005, 2025),
            mode=mode,
            season_len=3,
        )
        pieces = []
        for y in sorted(rec.year.unique()):
            b = Blender().fit(rec[rec.year != y])
            br = blended_records(rec[rec.year == y], b.weights)
            pieces.append(br)
        blend = pd.concat(pieces, ignore_index=True)
        cal = TercileCalibrator().fit(blend[["p0", "p1", "p2"]].to_numpy(), blend.obs_tercile.to_numpy())
        cal.save(cfg.artifact_dir / f"calib_{mode}_t2m.json")
        log(f"калибровка {mode}: {cal.n} записей")
        if mode == "seasonal":
            pt = PointDataset(cfg, cfg.region.lat_min + 0.5, cfg.region.lon_min + 0.5, cfg.zarr_store())
            pf = pt.predictor_frame()
            std = pt.seasonal_std("t2m", 3)
            pool = feature_columns_for(pf, "t2m", mode="seasonal")
            nn_year = {}
            for Y in range(2005, 2025):
                nn = PooledNN().fit(pf, std, pool, "seasonal", Y)
                if not nn.usable():
                    continue
                for tgt in std.index:
                    if tgt.year == Y:
                        Pn = nn.probs_for(pf, tgt - 1, pool, tgt.month, 1)
                        if Pn is not None:
                            nn_year[(tgt.year, tgt.month)] = Pn
            bmap = {
                (int(r.year), int(r.target_month)): (r[["p0", "p1", "p2"]].to_numpy(float), int(r.obs_tercile))
                for _, r in blend.iterrows()
            }
            Sy, Ny, oy = [], [], []
            for k, (ps, ob) in bmap.items():
                if k in nn_year:
                    Sy.append(ps)
                    Ny.append(nn_year[k])
                    oy.append(ob)
            if len(oy) >= 30:
                Sy, Ny, oy = np.array(Sy), np.array(Ny), np.array(oy)
                best_a, best_r = 0.0, 1e9
                for a in np.arange(0, 0.61, 0.05):
                    rr = rps_mean((1 - a) * Sy + a * Ny, oy)
                    if rr < best_r:
                        best_r, best_a = rr, float(a)
                (cfg.artifact_dir / "stack_seasonal_t2m.json").write_text('{"alpha": %s}' % round(best_a, 3))
                log(f"нейроядро: alpha={round(best_a, 3)}")


def ensure_point(cfg, world_dir, log):
    marker = Path(cfg.data_dir) / "ready.json"
    if marker.exists():
        log("данные точки уже готовы")
        return
    wcfg = world_config(world_dir)
    inside = (
        cfg.region.lat_min >= wcfg.region.lat_min
        and cfg.region.lat_max <= wcfg.region.lat_max
        and cfg.region.lon_min >= wcfg.region.lon_min
        and cfg.region.lon_max <= wcfg.region.lon_max
    )
    if not inside:
        log("скачиваю суточные наблюдения CPC для точки (1979–2026, обычно 2–6 минут)")
        fetch_cpc_daily(cfg, workers=4)
        log("суточные данные готовы")
        log("скачиваю почвенную влагу NCEP")
        fetch_soil(cfg)
        log("почва готова")
    _fit_artifacts(cfg, log)
    marker.write_text(json.dumps({"ok": True}))
    log("точка готова")


def _nn_probs(cfg, pf, std, pool, mode, targets):
    from agrocast.models.nn_kernel import PooledNN

    by_year = {}
    for t in targets:
        by_year.setdefault(t.year, []).append(t)
    out = {}
    for y, ts in by_year.items():
        nn = PooledNN().fit(pf, std, pool, mode, y)
        if not nn.usable():
            continue
        for t in ts:
            Pn = nn.probs_for(pf, t - 1, pool, t.month, 1)
            if Pn is not None:
                out[(t.year, t.month)] = Pn
    return out


def run_hindcast(cfg, lat, lon, start, mode, horizon, log):
    from agrocast.blend.blender import Blender, blended_records
    from agrocast.blend.calibration import TercileCalibrator
    from agrocast.features.dataset import PointDataset, feature_columns_for
    from agrocast.backtest.metrics import rps_mean

    start = pd.Period(str(start), "M")
    horizon = int(horizon)
    if mode == "seasonal":
        targets = [start + 3 * k for k in range(max(1, (horizon + 2) // 3))]
    else:
        targets = [start + k for k in range(horizon)]
    if targets[-1] > pd.Period("2024-12", "M"):
        raise ValueError("горизонт уходит за пределы честной проверки (до 2024-12): выберите более раннюю дату или меньший период")
    if targets[0] < pd.Period("2008-01", "M"):
        raise ValueError("проверка на истории доступна с 2008 года")
    log(f"проверка на истории: старт {start}, режим {mode}, целей {len(targets)}")
    rec = pd.read_parquet(cfg.artifact_dir / f"backtest_records_{mode}.parquet")
    if mode == "monthly":
        rec = rec[rec.lead == 1]
    tyears = sorted({int(t.year) for t in targets})
    frames = []
    for t in targets:
        g = rec[(rec.year == t.year) & (rec.target_month == t.month)]
        if not g.empty:
            frames.append(g)
    if not frames:
        raise ValueError("для этой даты нет записей проверки")
    cur = pd.concat(frames, ignore_index=True)
    past_years = sorted(int(y) for y in rec.year.unique() if y < max(tyears) and int(y) not in tyears)
    blender = Blender().fit(rec[~rec.year.isin(tyears)])
    past = []
    for t in past_years:
        bt = blended_records(rec[rec.year == t], Blender().fit(rec[~rec.year.isin(set(past_years) | {t})]).weights)
        past.append(bt)
    past = pd.concat(past, ignore_index=True) if past else None
    pt = PointDataset(cfg, lat, lon, cfg.zarr_store())
    pf = pt.predictor_frame()
    stds = {}
    for v in ("t2m", "tp"):
        stds[v] = pt.seasonal_std(v, 3) if mode == "seasonal" else pt.standardized(v)
    cal = None
    if past is not None:
        g = past[past.variable == "t2m"]
        if len(g) >= 60:
            cal = TercileCalibrator().fit(g[["p0", "p1", "p2"]].to_numpy(), g.obs_tercile.to_numpy())
    pool = feature_columns_for(pf, "t2m", mode=mode)
    alpha = 0.0
    nn_now = _nn_probs(cfg, pf, stds["t2m"], pool, mode, targets)
    if past is not None and mode == "seasonal":
        tmonths = sorted({int(t.month) for t in targets})
        past_targets = [pd.Period(year=y, month=m, freq="M") for y in past_years for m in tmonths]
        nn_past = _nn_probs(cfg, pf, stds["t2m"], pool, mode, past_targets)
        g = past[past.variable == "t2m"]
        Sy, Ny, oy = [], [], []
        for _, r in g.iterrows():
            k = (int(r.year), int(r.target_month))
            if k in nn_past:
                Sy.append(r[["p0", "p1", "p2"]].to_numpy(float))
                Ny.append(nn_past[k])
                oy.append(int(r.obs_tercile))
        if len(oy) >= 20:
            Sy, Ny, oy = np.array(Sy), np.array(Ny), np.array(oy)
            best_r = 1e9
            for a in np.arange(0, 0.61, 0.05):
                rr = rps_mean((1 - a) * Sy + a * Ny, oy)
                if rr < best_r:
                    best_r, alpha = rr, float(a)
    log(f"нейроядро: alpha={round(alpha, 2)} (обучено только на годах до {max(tyears)})")
    items = []
    for t in targets:
        block = {"year": int(t.year), "target_month": int(t.month)}
        for v in ("t2m", "tp"):
            g = cur[(cur.variable == v) & (cur.year == t.year) & (cur.target_month == t.month)]
            if g.empty:
                continue
            r = g.iloc[0]
            P = r[["p0", "p1", "p2"]].to_numpy(float)
            if v == "t2m" and cal is not None and cal.usable():
                P = cal.transform(P.reshape(1, -1))[0]
            if v == "t2m" and alpha > 0 and (t.year, t.month) in nn_now:
                P = (1 - alpha) * P + alpha * nn_now[(t.year, t.month)]
                P = P / P.sum()
            std = stds[v]
            if t not in std.index:
                continue
            mu = float(std.loc[t, "mu"])
            sd = float(std.loc[t, "sd"])
            z = float(std.loc[t, "z"])
            obs = int(r.obs_tercile)
            dom = int(np.argmax(P))
            block[v] = {
                "probs": [round(float(x), 3) for x in P],
                "dominant": dom,
                "obs": obs,
                "hit": bool(dom == obs),
                "fact": round(mu + sd * z, 1),
                "p50": round(mu + sd * float(r.q50), 1),
                "norm": round(mu, 1),
                "unit": "c" if v == "t2m" else "mm",
            }
        if "t2m" in block or "tp" in block:
            items.append(block)
    summary = {}
    for v in ("t2m", "tp"):
        hits = [it[v]["hit"] for it in items if v in it]
        summary[v] = {"hits": int(sum(hits)), "total": len(hits)}
    log(f"итог: температура {summary['t2m']['hits']}/{summary['t2m']['total']}, осадки {summary['tp']['hits']}/{summary['tp']['total']}")
    return {"kind": "hindcast", "start": str(start), "mode": mode, "lat": float(lat), "lon": float(lon), "items": items, "summary": summary}


class Job:
    def __init__(self, job_id, params):
        self.id = job_id
        self.params = params
        self.status = "running"
        self.log = []
        self.result = None
        self.error = None

    def add(self, line):
        self.log.append(line)


def run_job(job, world_dir, data_root):
    try:
        from agrocast.forecast.orchestrator import forecast_point

        lat = float(job.params["lat"])
        lon = float(job.params["lon"])
        kind = job.params.get("kind", "forecast")
        cfg, _ = point_config(world_dir, data_root, lat, lon)
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
            )
        job.status = "done"
        job.add("готово")
    except Exception as exc:
        job.status = "error"
        job.error = f"{type(exc).__name__}: {exc}"
        job.add("ошибка: " + job.error)
        traceback.print_exc()


def start_job(job, world_dir, data_root):
    t = threading.Thread(target=run_job, args=(job, world_dir, data_root), daemon=True)
    t.start()
    return job
