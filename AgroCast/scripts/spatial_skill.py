#!/usr/bin/env python3
"""Пространственный навык по локальному факту ячеек (T10).

Для каждой ячейки региона — полный локальный ряд наблюдений, локальные
терцильные границы и климат-бейзлайн; перекрытые месяцы/сезоны и
скоррелированные точки учитываются moving-block bootstrap (время) и
2x2 spatial-блоками. Артефакт grid-skill-v2: отчёт по всем комбинациям
mode/variable/lead + легаси-ключи для API.
"""
import argparse
import json
import sys
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from agrocast.core.settings import RuntimeSettings  # noqa: E402
from agrocast.backtest.engine import run_backtest  # noqa: E402
from agrocast.region import regions  # noqa: E402
from agrocast.skill import spatial  # noqa: E402


def parse_years(text):
    if "-" in text:
        a, b = text.split("-")
        return list(range(int(a), int(b) + 1))
    return [int(x) for x in text.split(",") if x.strip()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default="krai")
    ap.add_argument("--years", default="2010-2024")
    ap.add_argument("--start-months", default=",".join(str(m) for m in range(1, 13)))
    ap.add_argument("--leads-monthly", default="1,2,3,4,5,6")
    ap.add_argument("--mode", choices=("both", "seasonal", "monthly"), default="both")
    ap.add_argument("--max-cells", type=int, default=0)
    ap.add_argument("--n-boot", type=int, default=400)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default="", help="каталог артефакта (по умолчанию artifacts writable state/world)")
    a = ap.parse_args()

    years = parse_years(a.years)
    months = [int(x) for x in str(a.start_months).split(",") if str(x).strip()]
    leads_m = [int(x) for x in str(a.leads_monthly).split(",") if str(x).strip()]
    settings = RuntimeSettings.from_environment()
    config = settings.compute_config()
    store = config.zarr_store()

    cells = regions.region_cells(store, region=a.region)
    if a.max_cells and len(cells) > a.max_cells:
        step = max(1, len(cells) // a.max_cells)
        cells = cells[::step][: a.max_cells]
    if not cells:
        raise SystemExit(f"нет ячеек региона {a.region} в {store.path('fields_monthly')}")

    modes = []
    if a.mode in ("both", "seasonal"):
        modes.append("seasonal")
    if a.mode in ("both", "monthly"):
        modes.append("monthly")

    frames = []
    for k, cell in enumerate(cells, 1):
        pid, lat, lon = cell["id"], float(cell["lat"]), float(cell["lon"])
        for mode in modes:
            leads = leads_m if mode == "monthly" else [1]
            _, pipe = run_backtest(
                config, variables=("t2m", "tp"), start_months=months, leads=leads, years=years,
                mode=mode, lat=float(lat), lon=float(lon), save_artifacts=False, return_pipeline=True,
            )
            if pipe is None or pipe.empty:
                continue
            pipe = pipe.assign(mode=mode)
            frames.append(spatial.add_cell_frame(pipe, lat, lon, pid))
            print(f"[{k}/{len(cells)}] {pid} ({float(lat):.2f},{float(lon):.2f}) mode={mode}: +{len(pipe)}", flush=True)
    if not frames:
        raise SystemExit("пустая пайплайн-таблица: нет верификаций в выбранном окне")

    pipe_all = pd.concat(frames, ignore_index=True)
    leads_by_mode = {"monthly": leads_m, "seasonal": [1]}
    combos = spatial.summarize(pipe_all, modes=modes, leads=leads_by_mode, n_boot=a.n_boot, seed=a.seed,
                               artifact_dir=config.artifact_dir)
    by_point = spatial.per_point_summary(pipe_all)
    region_name = regions.REGIONS[a.region].get("name", a.region) if a.region in regions.REGIONS else a.region
    art = spatial.build_skill_artifact(
        combos, by_point, a.region, region_name, f"{years[0]}-{years[-1]}", len(cells),
        f"spatial_skill v2: {len(cells)} ячеек, локальные факты ячеек (терцильные границы и baseline по ячейке); {spatial.METHOD}",
    )
    out_dir = Path(a.out) if a.out else settings.state_dir / "artifacts"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{a.region}_grid_skill.json"
    spatial.write_artifact(out, art)
    print(json.dumps({"artifact": str(out), "verifications": art["verifications"], "combos": len(combos),
                      "n_cells": len(cells), "years": art["years"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
