"""По-годовая «счёт-фактура»: в скольких годах система лучше
климатологии (RPSS > 0) по температуре и осадкам — за последние 10 лет
и за всю доступную выборку.

Две системы, один протокол (leave-one-year-out, без подглядывания):
  НОВАЯ — 9 моделей, веса «свежести», изотоника + конформная калибровка
         (строки из реестра доверия trust_ledger_*.parquet);
  СТАРАЯ — 6 моделей, равномерные веса, без калибровок
         (те же записи backtest'а, отфильтрованные по именам моделей).

Запуск:  python -m scripts.yearly_scorecard  (из папки AgroCast)
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from agrocast.serve.pipeline import world_config  # noqa: E402
from agrocast.backtest.metrics import rps_rows  # noqa: E402

OLD_MODELS = ["clim", "analog", "ridge", "gbm", "ridge_land", "ridge_ocean"]
YEARS = range(2015, 2025)


def _rpss(df):
    P = df[["p0", "p1", "p2"]].to_numpy(float)
    obs = df["obs_tercile"].to_numpy(int)
    rps = rps_rows(P, obs)
    rps_c = rps_rows(np.tile([1 / 3.0] * 3, (len(obs), 1)), obs)
    return round(1.0 - float(rps.mean()) / float(rps_c.mean()), 3), float((P.argmax(axis=1) == obs).mean())


def old_loy(rec, year, variable):
    from agrocast.blend.blender import Blender, blended_records

    r = rec[(rec.year != year) & (rec.model.isin(OLD_MODELS)) & (rec.variable == variable)]
    b = Blender(half_life_years=0.0).fit(r)
    ry = rec[(rec.year == year) & (rec.model.isin(OLD_MODELS)) & (rec.variable == variable)]
    return blended_records(ry, b.weights)


def main():
    wc = world_config(BASE / "world")
    from agrocast.backtest.engine import load_records
    from agrocast.skill.ledger import load_ledger

    out = {}
    for mode in ("seasonal", "monthly"):
        rec = load_records(wc, mode)
        led, _ = load_ledger(wc, mode)
        rows = {}
        for y in YEARS:
            rows[y] = {}
            for v in ("t2m", "tp"):
                ln = led[(led.year == y) & (led.variable == v)]
                lo = old_loy(rec, y, v)
                rows[y][v] = {
                    "new_rpss": _rpss(ln)[0], "new_hit": round(_rpss(ln)[1], 3), "new_n": int(len(ln)),
                    "old_rpss": _rpss(lo)[0] if len(lo) else None,
                    "old_hit": round(_rpss(lo)[1], 3) if len(lo) else None,
                }
        # итог по 10-летнему окну (все годы вместе)
        window = {}
        for v in ("t2m", "tp"):
            ln = led[(led.year.isin(list(YEARS))) & (led.variable == v)]
            lo = pd.concat([old_loy(rec, y, v) for y in YEARS], ignore_index=True)
            window[v] = {
                "new_rpss": _rpss(ln)[0], "new_hit": round(_rpss(ln)[1], 3), "new_n": int(len(ln)),
                "old_rpss": _rpss(lo)[0] if len(lo) else None,
                "old_hit": round(_rpss(lo)[1], 3) if len(lo) else None,
            }
        all_years = sorted(led.year.unique())
        full = {}
        for v in ("t2m", "tp"):
            ln = led[led.variable == v]
            lo = pd.concat([old_loy(rec, y, v) for y in all_years], ignore_index=True)
            full[v] = {
                "new_rpss": _rpss(ln)[0], "new_hit": round(_rpss(ln)[1], 3), "new_n": int(len(ln)),
                "old_rpss": _rpss(lo)[0] if len(lo) else None,
                "old_hit": round(_rpss(lo)[1], 3) if len(lo) else None,
            }
        out[mode] = {"years": rows, "window_2015_2024": window, "full": full, "full_years": [int(y) for y in all_years]}

    p = wc.artifact_dir / "yearly_scorecard.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=1))

    for mode in ("seasonal", "monthly"):
        d = out[mode]
        led, _ = load_ledger(wc, mode)
        print(f"\n================= {mode.upper()} =================")
        print(f"{'год':>5} | {'T нов':>7} {'T стар':>7} | {'P нов':>7} {'P стар':>7} | {'T pop':>6} {'P pop':>6}")
        for y in YEARS:
            r = d["years"][y]
            print(
                f"{y:>5} | {r['t2m']['new_rpss']:>+7.3f} {str(r['t2m']['old_rpss']):>7} "
                f"| {r['tp']['new_rpss']:>+7.3f} {str(r['tp']['old_rpss']):>7} "
                f"| {round(r['t2m']['new_hit']*100):>5.0f}% {round(r['tp']['new_hit']*100):>5.0f}%"
            )
        w = d["window_2015_2024"]
        t_new = sum(1 for y in YEARS if d["years"][y]["t2m"]["new_rpss"] > 0)
        t_old = sum(1 for y in YEARS if (d["years"][y]["t2m"]["old_rpss"] or 0) > 0)
        p_new = sum(1 for y in YEARS if d["years"][y]["tp"]["new_rpss"] > 0)
        p_old = sum(1 for y in YEARS if (d["years"][y]["tp"]["old_rpss"] or 0) > 0)
        print(f"{'ИТОГ':>5} | {w['t2m']['new_rpss']:>+7.3f} {str(w['t2m']['old_rpss']):>7} "
              f"| {w['tp']['new_rpss']:>+7.3f} {str(w['tp']['old_rpss']):>7} "
              f"| {round(w['t2m']['new_hit']*100):>5.0f}% {round(w['tp']['new_hit']*100):>5.0f}%")
        print(f"  лет лучше климатологии (RPSS>0) из 10:  T: новый {t_new} / старый {t_old}   |   P: новый {p_new} / старый {p_old}")
        print(f"  10-летний RPSS:  T: новый {w['t2m']['new_rpss']:+.3f} (старый {w['t2m']['old_rpss']:+.3f})   |   "
              f"P: новый {w['tp']['new_rpss']:+.3f} (старый {w['tp']['old_rpss']:+.3f})")
        print(f"  попадание доминанты 10 лет:  T: {w['t2m']['new_hit']*100:.0f}% (наугад 33%)   |   "
              f"P: {w['tp']['new_hit']*100:.0f}% (наугад 33%)")
        f = d["full"]
        fy = d["full_years"]
        t_pos = p_pos = 0
        for y in fy:
            ln = led[(led.year == y) & (led.variable == "t2m")]
            if _rpss(ln)[0] > 0:
                t_pos += 1
            lp = led[(led.year == y) & (led.variable == "tp")]
            if _rpss(lp)[0] > 0:
                p_pos += 1
        print(f"  ПОЛНАЯ ВЫБОРКА {fy[0]}–{fy[-1]} ({len(fy)} лет):  T: новый {f['t2m']['new_rpss']:+.3f} (старый {f['t2m']['old_rpss']:+.3f}), "
              f"лет RPSS>0: {t_pos}/{len(fy)}, попадание {f['t2m']['new_hit']*100:.0f}%   |   "
              f"P: новый {f['tp']['new_rpss']:+.3f} (старый {f['tp']['old_rpss']:+.3f}), лет RPSS>0: {p_pos}/{len(fy)}, попадание {f['tp']['new_hit']*100:.0f}%")


if __name__ == "__main__":
    main()
