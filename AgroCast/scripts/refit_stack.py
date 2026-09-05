import sys
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from agrocast.serve.pipeline import world_config


def main(world_dir=None):
    wc = world_config(world_dir)
    from agrocast.blend.blender import Blender, attach_obs, blended_records
    from agrocast.blend.calibration import gated_calibrator
    from agrocast.blend.nn_stack import mix, nn_map, row_keys, save_alpha, select_alpha
    from agrocast.features.dataset import PointDataset

    for mode in ("seasonal", "monthly"):
        rec = pd.read_parquet(wc.artifact_path(f"backtest_records_{mode}.parquet"))
        pieces = []
        for y in sorted(rec.year.unique()):
            b = Blender(half_life_years=5.0).fit(rec[rec.year != y])
            pieces.append(blended_records(rec[rec.year == y], b.weights))
        blend = attach_obs(pd.concat(pieces, ignore_index=True), rec)
        _pt = PointDataset(wc, wc.region.lat_min + 0.5, wc.region.lon_min + 0.5, wc.zarr_store())
        for v in ("t2m", "tp"):
            sub = blend[blend.variable == v]
            nnmap = nn_map(_pt, v, mode, sorted(sub["year"].unique()))
            a = select_alpha(_pt, v, mode, blend, nnmap=nnmap)
            save_alpha(wc, mode, v, a)
            P = sub[["p0", "p1", "p2"]].to_numpy(float)
            if a > 0:
                P = mix(P, row_keys(sub), nnmap, a)
            cal = gated_calibrator(P, sub.obs_tercile.to_numpy(), years=sub["year"].to_numpy())
            cal_path = wc.artifact_dir / f"calib_{mode}_{v}.json"
            cal.save(cal_path)
            print(f"{mode} {v}: alpha={a}, calib n={cal.n}")


if __name__ == "__main__":
    main()
