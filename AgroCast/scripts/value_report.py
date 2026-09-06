from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from agrocast.core.settings import RuntimeSettings

MODES = ("seasonal", "monthly")
VARS = ("t2m", "tp")
SEASONS = ("DJF", "MAM", "JJA", "SON")
SEASON_RU = {"DJF": "зима (DJF)", "MAM": "весна (MAM)", "JJA": "лето (JJA)", "SON": "осень (SON)"}
TER_KEYS = ("below", "normal", "above")
TER_RU = ("ниже нормы", "около нормы", "выше нормы")


def _rps_rows(probs, obs):
    probs = np.asarray(probs, float)
    obs = np.asarray(obs, int)
    c = np.cumsum(probs, axis=1)
    o = np.zeros_like(c)
    o[np.arange(len(obs)), obs] = 1.0
    o = np.cumsum(o, axis=1)
    return ((c - o) ** 2).sum(axis=1)


def _r(x):
    return None if x is None else float(round(float(x), 4))


def _probs(sub):
    return sub[["p0", "p1", "p2"]].to_numpy(float)


def _metrics(sub):
    n = int(len(sub))
    if n == 0:
        return {"n": 0, "rps": None, "rps_clim": None, "rpss": None, "hit": None}
    rps = float(sub["rps"].mean())
    rc = float(sub["rps_c"].mean())
    rpss = 1.0 - rps / rc if rc > 1e-9 else None
    hit = float(sub["hit"].mean())
    return {"n": n, "rps": _r(rps), "rps_clim": _r(rc), "rpss": _r(rpss), "hit": _r(hit)}


def _calib(sub):
    obs = sub["obs_tercile"].to_numpy(int)
    p = _probs(sub)
    freq = {}
    claimed = {}
    ece_parts = {}
    ece = 0.0
    for k, key in enumerate(TER_KEYS):
        fk = float((obs == k).mean())
        pk = float(p[:, k].mean())
        mk = obs == k
        cond = float(p[mk, k].mean()) if mk.any() else 0.0
        part = fk * abs(cond - fk)
        ece += part
        freq[key] = _r(fk)
        claimed[key] = _r(pk)
        ece_parts[key] = _r(part)
    return {"claimed": claimed, "freq": freq, "ece": _r(ece), "ece_parts": ece_parts,
            "coverage": _r(float(sub["in_corridor_c"].mean()))}


def segment_stats(df, mode, variable):
    sub = df[(df["mode"] == mode) & (df["variable"] == variable)]
    m = _metrics(sub)
    if m["n"] == 0:
        return None
    win = float((sub["rps"] < sub["rps_c"]).mean())
    out = dict(m)
    out["win"] = _r(win)
    out.update(_calib(sub))
    return out


def years_block(df, mode="seasonal", variable="t2m"):
    sub = df[(df["mode"] == mode) & (df["variable"] == variable)].copy()
    rows = []
    for y in sorted(int(x) for x in sub["year"].unique()):
        g = _metrics(sub[sub["year"] == y])
        rows.append({"year": int(y), "n": g["n"], "rpss": g["rpss"], "hit": g["hit"]})
    if not rows:
        return {"rows": [], "n_years": 0, "positive_years": 0,
                "first_decade_rpss": None, "second_decade_rpss": None,
                "best": None, "worst": None}
    d1 = _metrics(sub[sub["year"] <= 2014])
    d2 = _metrics(sub[sub["year"] >= 2015])
    best = max(rows, key=lambda r: r["rpss"])
    worst = min(rows, key=lambda r: r["rpss"])
    return {
        "rows": rows,
        "n_years": len(rows),
        "positive_years": int(sum(1 for r in rows if r["rpss"] is not None and r["rpss"] > 0)),
        "first_decade_rpss": d1["rpss"],
        "second_decade_rpss": d2["rpss"],
        "best": {"year": best["year"], "rpss": best["rpss"], "hit": best["hit"]},
        "worst": {"year": worst["year"], "rpss": worst["rpss"], "hit": worst["hit"]},
    }


def decomposition(df, mode="seasonal", variable="t2m"):
    sub = df[(df["mode"] == mode) & (df["variable"] == variable)]
    obs = sub["obs_tercile"].to_numpy(int)
    rc = float(sub["rps_c"].mean())
    greedy = float(_rps_rows(np.tile([0.0, 0.0, 1.0], (len(obs), 1)), obs).mean())
    blend = float(sub["brps"].mean())
    product = float(sub["rps"].mean())
    stages = {}
    for key, r in (("uniform", rc), ("greedy_warm", greedy), ("blend", blend), ("product", product)):
        stages[key] = {"rps": _r(r), "rpss": _r(1.0 - r / rc) if rc > 1e-9 else None,
                       "gain": _r(rc - r)}
    return stages


def season_table(df):
    out = {}
    for mode in MODES:
        out[mode] = {}
        sub = df[df["mode"] == mode]
        for s in SEASONS:
            out[mode][s] = {}
            for v in VARS:
                g = _metrics(sub[(sub["variable"] == v) & (sub["season"] == s)])
                out[mode][s][v] = {"n": g["n"], "rpss": g["rpss"], "hit": g["hit"]}
    return out


def build_report(df):
    segments = {}
    for mode in MODES:
        for v in VARS:
            segments[f"{mode}_{v}"] = segment_stats(df, mode, v)
    years = {"seasonal_t2m": years_block(df, "seasonal", "t2m"),
             "monthly_t2m": years_block(df, "monthly", "t2m")}
    over = _calib(df)
    ece_all = over["ece"]
    cov_all = over["coverage"]
    rep = {
        "title": "Паспорт навыка AgroCast",
        "generated_at": time.strftime("%Y-%m-%d %H:%M"),
        "source": "data/audit/audit_records.csv ← scripts.audit_full grid (КРА, walk-forward 2005–2024)",
        "n_points": int(df["point"].nunique()),
        "verifications": int(len(df)),
        "years_span": f"{int(df['year'].min())}-{int(df['year'].max())}",
        "segments": segments,
        "years": years,
        "decomposition": {"seasonal_t2m": decomposition(df, "seasonal", "t2m")},
        "seasons": season_table(df),
        "notes": [
            "навык измерен и подтверждён на терцильных вероятностях (категории ниже/около/выше нормы), RPS/RPSS считаются по терцильным прогнозам; «лучше климатологии» — доля отдельных прогнозов с RPS < RPS равномерной климатологии",
            "часть навыка температуры объясняется тёплым трендом: жадный климат «всегда выше нормы» уже даёт положительный RPSS (строки декомпозиции) — это оценка вклада тренда, а не прогностической системы",
            "осадки (tp): сезонный и месячный RPSS ≤ 0 — навык не подтверждён, уровень климатологии; в продукте поле осадков помечено как связность прогноза, не подтверждённый навык",
            f"калибровка по всем 26 880 верификациям: ECE = {ece_all:.4f}, покрытие конформного коридора P10–P90 = {100 * cov_all:.1f}% при заявленных 80%",
            "walk-forward 2005–2024 без подглядывания в будущее: каждый год — прогноз по данным только прошлых лет",
            "география: 28 ячеек 0.5° Краснодарского края (сетка krai_grid.json); выводы валидны для юга России в границах бокса данных",
        ],
    }
    return rep


def _f_num(v, nd=4):
    if v is None:
        return "—"
    return f"{v:.{nd}f}"


def _f_sign(v, nd=4):
    if v is None:
        return "—"
    if float(v) == 0.0:
        return f"{v:.{nd}f}"
    return f"{v:+.{nd}f}"


def _fmt_hit(v):
    if v is None:
        return "—"
    return f"{100 * v:.1f}%"


def _rows_block(rows, year_key="год"):
    lines = [f"| {year_key} | n | RPSS | hit |", "|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['year']} | {r['n']} | {_f_sign(r['rpss'])} | {_f_num(r['hit'])} |")
    return "\n".join(lines)


def _seg_label(key):
    mode, v = key.split("_")
    return f"{'сезонный' if mode == 'seasonal' else 'месячный'} {v}"


def render_md(rep):
    L = []
    L.append(f"# {rep['title']}")
    L.append("")
    L.append(f"*Сгенерировано: {rep['generated_at']} · {rep['source']}*")
    L.append(f"*{rep['n_points']} точек × {rep['years_span']} (walk-forward) · {rep['verifications']:,} верификаций*".replace(",", " "))
    L.append("")
    L.append("## 1. Сегменты")
    L.append("")
    L.append("| сегмент | n | RPS | RPS клим | RPSS | hit | лучше климатологии | ECE | покрытие P10–P90 |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for key, s in rep["segments"].items():
        if s is None:
            continue
        L.append(f"| {_seg_label(key)} | {s['n']} | {_f_num(s['rps'])} | {_f_num(s['rps_clim'])} "
                 f"| {_f_sign(s['rpss'])} | {_f_num(s['hit'])} | {_f_num(s['win'])} "
                 f"| {_f_num(s['ece'])} | {_f_num(s['coverage'])} |")
    L.append("")
    L.append("## 2. По годам — сезонная t2m (продукт)")
    L.append("")
    y = rep["years"]["seasonal_t2m"]
    if y["rows"]:
        L.append(_rows_block(y["rows"]))
        L.append("")
        L.append(f"Лет с положительным RPSS: **{y['positive_years']} из {y['n_years']}** "
                 f"(провалы — см. отрицательные строки). Десятилетия: "
                 f"2005–2014 {_f_sign(y['first_decade_rpss'])} → 2015–2024 {_f_sign(y['second_decade_rpss'])}. "
                 f"Лучший год: {y['best']['year']} ({_f_sign(y['best']['rpss'])}), худший: {y['worst']['year']} ({_f_sign(y['worst']['rpss'])}).")
    else:
        L.append("нет данных в записях аудита")
    L.append("")
    L.append("## 3. По годам — месячная t2m (продукт, lead 1)")
    L.append("")
    y2 = rep["years"]["monthly_t2m"]
    if y2["rows"]:
        L.append(_rows_block(y2["rows"]))
        L.append("")
        L.append(f"Лет с положительным RPSS: **{y2['positive_years']} из {y2['n_years']}**. "
                 f"Лучший год: {y2['best']['year']} ({_f_sign(y2['best']['rpss'])}), худший: {y2['worst']['year']} ({_f_sign(y2['worst']['rpss'])}).")
    else:
        L.append("нет данных в записях аудита")
    L.append("")
    L.append("## 4. Декомпозиция — сезонная t2m (средний RPS против равномерной климатологии)")
    L.append("")
    L.append("| прогноз | средний RPS | RPSS против климата |")
    L.append("|---|---|---|")
    d = rep["decomposition"]["seasonal_t2m"]
    labels = {
        "uniform": "равномерная климатология [1/3, 1/3, 1/3]",
        "greedy_warm": "жадный климат «всегда выше нормы» [0, 0, 1] — оценка вклада тёплого тренда",
        "blend": "чистый ансамблевый бленд (без NN-mix и калибровки)",
        "product": "продукт (бленд + NN-mix + калибровка) — «проверка прошлого»",
    }
    for key, st in d.items():
        L.append(f"| {labels[key]} | {_f_num(st['rps'])} | {_f_sign(st['rpss'])} |")
    L.append("")
    L.append("## 5. Калибровка — сезонная t2m (продукт)")
    L.append("")
    s = rep["segments"]["seasonal_t2m"]
    L.append("| терцель | заявленная вероятность | фактическая частота | вклад в ECE |")
    L.append("|---|---|---|---|")
    for k, key in enumerate(TER_KEYS):
        L.append(f"| {TER_RU[k]} ({key}) | {_f_num(s['claimed'][key])} "
                 f"| {_f_num(s['freq'][key])} | {_f_num(s['ece_parts'][key])} |")
    L.append("")
    L.append(f"ECE = **{_f_num(s['ece'])}** · покрытие конформного коридора P10–P90 = **{_fmt_hit(s['coverage'])}**.")
    L.append("")
    L.append("## 6. По сезонам года")
    L.append("")
    for mode in MODES:
        L.append(f"**{'сезонный' if mode == 'seasonal' else 'месячный'} режим**")
        L.append("")
        L.append("| сезон | n (t2m) | RPSS t2m | hit t2m | n (tp) | RPSS tp | hit tp |")
        L.append("|---|---|---|---|---|---|---|")
        for ssn in SEASONS:
            t = rep["seasons"][mode][ssn]["t2m"]
            p = rep["seasons"][mode][ssn]["tp"]
            L.append(f"| {SEASON_RU[ssn]} | {t['n']} | {_f_sign(t['rpss'])} | {_f_num(t['hit'])} "
                     f"| {p['n']} | {_f_sign(p['rpss'])} | {_f_num(p['hit'])} |")
        L.append("")
    L.append("## Честно")
    L.append("")
    for i, note in enumerate(rep["notes"], 1):
        L.append(f"{i}. {note}")
    L.append("")
    md = "\n".join(L)
    return md


def write_outputs(rep, md_path, json_path):
    settings = RuntimeSettings.from_environment()
    md_path = settings.writable_path(md_path)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(render_md(rep), encoding="utf-8")
    json_path = settings.writable_path(json_path)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    return md_path, json_path


def main(argv=None):
    settings = RuntimeSettings.from_environment()
    ap = argparse.ArgumentParser()
    ap.add_argument("--records", default=str(settings.state_dir / "audit/audit_records.csv"))
    ap.add_argument("--md", default=str(settings.state_dir / "audit/value_report.md"))
    ap.add_argument("--json", default=str(settings.state_dir / "compute/artifacts/value_report.json"))
    args = ap.parse_args(argv)
    if not Path(args.records).exists():
        print("нет записей аудита: сначала запустите python -m scripts.audit_full grid --workers 2"
              f" (ожидался файл {args.records})", file=sys.stderr)
        return 2
    df = pd.read_csv(args.records)
    rep = build_report(df)
    md_path, json_path = write_outputs(rep, args.md, args.json)
    seg = rep["segments"]
    s_t2m = seg["seasonal_t2m"]
    m_t2m = seg["monthly_t2m"]
    y = rep["years"]["seasonal_t2m"]
    d = rep["decomposition"]["seasonal_t2m"]
    print(f"[value_report] точек: {rep['n_points']}, верификаций: {rep['verifications']:,}"
          .replace(",", " "))
    print(f"[value_report] сезонная t2m: RPSS {s_t2m['rpss']:+.4f}, hit {s_t2m['hit']:.4f}, "
          f"лучше климата {s_t2m['win']:.4f}, ECE {s_t2m['ece']:.4f}, покрытие {s_t2m['coverage']:.4f}")
    print(f"[value_report] месячная t2m: RPSS {m_t2m['rpss']:+.4f}, hit {m_t2m['hit']:.4f}, "
          f"лучше климата {m_t2m['win']:.4f}")
    print(f"[value_report] tp: сезонный {seg['seasonal_tp']['rpss']:+.4f}, "
          f"месячный {seg['monthly_tp']['rpss']:+.4f} (навык не подтверждён)")
    print(f"[value_report] лет RPSS>0: сезонная t2m {y['positive_years']}/{y['n_years']}, "
          f"десятилетия {y['first_decade_rpss']:+.4f} → {y['second_decade_rpss']:+.4f}, "
          f"лучший {y['best']['year']}, худший {y['worst']['year']}")
    print(f"[value_report] декомпозиция RPS: клим {d['uniform']['rps']:.4f} → жадный {d['greedy_warm']['rps']:.4f}"
          f" (RPSS {d['greedy_warm']['rpss']:+.4f}) → бленд {d['blend']['rps']:.4f}"
          f" (RPSS {d['blend']['rpss']:+.4f}) → продукт {d['product']['rps']:.4f}"
          f" (RPSS {d['product']['rpss']:+.4f})")
    print(f"[value_report] готово: {md_path}")
    print(f"[value_report] готово: {json_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
