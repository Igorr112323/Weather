"""Переобучение мировых артефактов: новый ансамбль (9 моделей),
веса с полупериодом 5 лет, изотоника + конформная калибровка по
каждой переменной, реестр доверия.

Запуск:  python -m scripts.refit_world  (из папки AgroCast)
"""
import os
import sys
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from agrocast.serve.pipeline import world_config  # noqa: E402


def main(world_dir=None, years=(2004, 2025), half_life=5.0):
    w = Path(world_dir) if world_dir else BASE / "world"
    wc = world_config(w)
    from agrocast.backtest.engine import run_backtest, load_records, records_name
    from agrocast.blend.blender import Blender, blended_records, attach_obs, season_of
    from agrocast.blend.calibration import TercileCalibrator
    from agrocast.blend.conformal import ConformalQuantileCalibrator
    from agrocast.backtest.metrics import rpss

    for mode, leads in [("seasonal", [1]), ("monthly", list(range(1, 7)))]:
        print(f"== backtest {mode} years {years[0]}..{years[1]-1} ==")
        rec = None
        reuse = os.environ.get("REUSE_RECORDS") == "1" or os.environ.get(f"REUSE_RECORDS_{mode.upper()}") == "1"
        if reuse and (wc.artifact_dir / records_name(mode)).exists():
            rec = load_records(wc, mode)
            print("  (использую уже посчитанные записи)")
        if rec is None or rec.empty:
            rec = run_backtest(
            wc,
            variables=("t2m", "tp"),
            start_months=list(range(1, 13)),
            leads=leads,
            years=range(*years),
            mode=mode,
            season_len=3,
            half_life_years=half_life,
        )
        if rec is None or rec.empty:
            print("  нет записей")
            continue
        # LOY-бленд для калибровки (без подглядывания)
        pieces = []
        for y in sorted(rec.year.unique()):
            b = Blender(half_life_years=half_life).fit(rec[rec.year != y])
            pieces.append(blended_records(rec[rec.year == y], b.weights))
        blend = pd.concat(pieces, ignore_index=True)
        blend = attach_obs(blend, rec)
        blend["season"] = blend["target_month"].map(season_of)
        from agrocast.blend.nn_stack import select_alpha, save_alpha, nn_map, mix, row_keys
        from agrocast.backtest.engine import region_center
        from agrocast.features.dataset import PointDataset

        pt = PointDataset(wc, *region_center(wc.region), wc.zarr_store())
        alphas = {}
        nnmaps = {}
        for v in ("t2m", "tp"):
            sub = blend[blend.variable == v]
            nnmap = nn_map(pt, v, mode, sorted(sub["year"].unique()))
            nnmaps[v] = nnmap
            a = select_alpha(pt, v, mode, blend, nnmap=nnmap)
            alphas[v] = a
            save_alpha(wc, mode, v, a)
            print(f"  нейроядро {mode} {v}: alpha={a}")
        for v in ("t2m", "tp"):
            sub = blend[blend.variable == v]
            P = sub[["p0", "p1", "p2"]].to_numpy(float)
            if alphas[v] > 0:
                P = mix(P, row_keys(sub), nnmaps[v], alphas[v])
            cal = TercileCalibrator().fit(P, sub["obs_tercile"].to_numpy())
            cal.save(wc.artifact_dir / f"calib_{mode}_{v}.json")
            ccal = ConformalQuantileCalibrator().fit(sub)
            ccal.save(wc.artifact_dir / f"conformal_{mode}_{v}.json")
            P = cal.transform(P) if cal.usable() else P
            skill = rpss(P, sub["obs_tercile"].to_numpy(int))
            d = ccal.deltas.get(v, {})
            print(f"  {v}: calib n={cal.n} rpss_blend_cal={skill:.3f} conformal={ {k: [round(x, 2) for x in val] for k, val in d.items() if val} }")
        from agrocast.blend.regime_clim import RegimeClimatology, SPECS

        if SPECS.get(mode):
            rc = RegimeClimatology.fit_history(mode, pt)
            rc.save_live(wc.artifact_dir / f"regimeclim_{mode}.json")
            print(f"  regimeclim {mode}: группы {sorted(set(g for _, g in rc.history))}")
        from agrocast.skill.ledger import build_ledger, save_ledger

        s = save_ledger(build_ledger(rec, mode=mode, config=wc, half_life_years=half_life), wc, mode)
        if s:
            print(f"  ledger {mode}: n={s['overall']['n']} rpss={s['overall']['rpss']} p80={s['p80_coverage']}")
    print("done: артефакты переобучены")


if __name__ == "__main__":
    import json

    args = {}
    if len(sys.argv) > 1:
        args = json.loads(sys.argv[1])
    main(**args)
