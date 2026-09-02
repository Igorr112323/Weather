"""A/B-тест точности: старая система (6 моделей, равномерные веса)
против новой (9 моделей, веса по «свежести» 5 лет, изотоника,
конформная калибровка). Всё — на одних и тех же записях backtest'а,
протокол leave-one-year-out без подглядывания.

Запуск:  python -m scripts.ab_skill  (из папки AgroCast)
Результат: world/artifacts/ab_skill.json + таблица в консоль.
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from agrocast.serve.pipeline import world_config  # noqa: E402

OLD_MODELS = ["clim", "analog", "ridge", "gbm", "ridge_land", "ridge_ocean"]


def _metrics(blend):
    out = {}
    for v, g in blend.groupby("variable"):
        P = g[["p0", "p1", "p2"]].to_numpy(float)
        obs = g["obs_tercile"].to_numpy(int)
        dom = P.argmax(axis=1)
        conf = P.max(axis=1) > 0.45
        from agrocast.backtest.metrics import rps_rows

        rps = rps_rows(P, obs)
        rps_clim = rps_rows(np.tile([1 / 3.0] * 3, (len(obs), 1)), obs)
        rpss = 1.0 - float(rps.mean()) / float(rps_clim.mean())
        q = g[["q10", "q50", "q90"]].to_numpy(float)
        o = g["obs_z"].to_numpy(float)
        out[v] = {
            "n": int(len(g)),
            "rpss": round(rpss, 3),
            "hit": round(float((dom == obs).mean()), 3),
            "hit_conf": round(float((dom[conf] == obs[conf]).mean()), 3) if conf.sum() >= 10 else None,
            "coverage80": round(float(np.mean((q[:, 0] - 1e-9 <= o) & (o <= q[:, 2] + 1e-9))), 3),
        }
    return out


def _clim_metrics(rec):
    from agrocast.backtest.metrics import rps_rows

    out = {}
    clim_rows = rec[rec.model == "clim"]
    for v, g in clim_rows.groupby("variable"):
        obs = g["obs_tercile"].to_numpy(int)
        P = g[["p0", "p1", "p2"]].to_numpy(float)
        rps = rps_rows(P, obs)
        rps_clim = rps_rows(np.tile([1 / 3.0] * 3, (len(obs), 1)), obs)
        q = g[["q10", "q90"]].to_numpy(float)
        o = g["obs_z"].to_numpy(float)
        out[v] = {
            "n": int(len(g)),
            "rpss": round(1.0 - float(rps.mean()) / float(rps_clim.mean()), 3),
            "hit": round(float((P.argmax(axis=1) == obs).mean()), 3),
            "coverage80": round(float(np.mean((q[:, 0] - 1e-9 <= o) & (o <= q[:, 1] + 1e-9))), 3),
        }
    return out


def loy_blend(rec, models, half_life):
    from agrocast.blend.blender import Blender, blended_records, attach_obs

    pieces = []
    for y in sorted(rec.year.unique()):
        r_all = rec[rec.year != y]
        r = r_all if models is None else r_all[r_all.model.isin(models)]
        b = Blender(half_life_years=half_life).fit(r)
        ry = rec[rec.year == y] if models is None else rec[(rec.year == y) & (rec.model.isin(models))]
        pieces.append(blended_records(ry, b.weights))
    return attach_obs(pd.concat(pieces, ignore_index=True), rec)


def main(world_dir=None):
    w = Path(world_dir) if world_dir else BASE / "world"
    wc = world_config(w)
    from agrocast.backtest.engine import load_records, skill_summary
    from agrocast.skill.ledger import load_ledger

    out = {}
    for mode in ("seasonal", "monthly"):
        rec = load_records(wc, mode)
        if rec is None or rec.empty:
            continue
        old = loy_blend(rec, OLD_MODELS, 0.0)
        led, summary = load_ledger(wc, mode)
        row = {
            "climatology_baseline": _clim_metrics(rec),
            "old_system_6models_uniform": _metrics(old),
            "new_system_9models_fresh": {
                k: summary[k] for k in ("t2m", "tp", "overall", "p80_coverage") if k in summary
            },
            "new_system_coverage_before_conformal": {
                v: m["coverage80"] for v, m in _metrics(loy_blend(rec, None, 5.0)).items()
            },
            "per_model_rpss": {
                r["model"]: round(r["rpss"], 3)
                for _, r in skill_summary(rec).iterrows()
            },
        }
        out[mode] = row
    p = wc.artifact_dir / "ab_skill.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print(json.dumps(out, ensure_ascii=False, indent=1))
    print("\n=== ЧИТАБЕЛЬНАЯ ТАБЛИЦА ===")
    for mode, row in out.items():
        print(f"\n[{mode}]")
        for label, key in (
            ("БАЗЛАЙН: адаптивная климатология", "climatology_baseline"),
            ("СТАРЫЕ 6 моделей (равн. веса)", "old_system_6models_uniform"),
            ("НОВЫЕ 9 моделей + калибровки", "new_system_9models_fresh"),
        ):
            d = row[key]
            for v in ("t2m", "tp"):
                if v in d:
                    m = d[v]
                    cov = m.get("p80_coverage") if v == "t2m" else d.get("p80_coverage")
                    if key == "new_system_9models_fresh":
                        cov = d.get("p80_coverage")
                    else:
                        cov = m.get("coverage80")
                    hc = m.get("hit_conf")
                    print(
                        f"  {label:34s} {v:4s} RPSS {m['rpss']:+.3f} | попадание {m['hit']*100:.0f}% | "
                        f"уверенные {str(round((hc or 0)*100))+'%':>4s} | P10-90 покрытие {(cov if cov is not None else 0)*100:.0f}%"
                    )


if __name__ == "__main__":
    main()
