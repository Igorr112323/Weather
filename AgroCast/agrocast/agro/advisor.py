import numpy as np

from agrocast.agro import units

CROPS = [
    {"key": "maize", "name": "Кукуруза на зерно", "sow_months": (4, 5), "t_window": (10.0, 15.0)},
]

GTK_RISK = [(0.5, "чрезвычайная засуха"), (1.0, "засуха"), (1.3, "угроза засухи"), (2.0, "благополучно"), (99.0, "избыточно влажно")]
HEAT_T = 30.0


def gtk_class(g):
    if g is None or not np.isfinite(g):
        return None
    for thr, label in GTK_RISK:
        if g < thr:
            return label
    return GTK_RISK[-1][1]


def drought_block(tp_probs, tp_q_mm, tp_norm_mm, gtk_p50=None, spi_p50=None):
    p_dry = float(tp_probs.get("below", 1.0 / 3.0))
    deficit = float(tp_norm_mm - tp_q_mm.get("p50", tp_norm_mm)) if tp_norm_mm else None
    risk = p_dry
    if gtk_p50 is not None and np.isfinite(gtk_p50):
        if gtk_p50 < 1.0:
            risk += 0.2
        elif gtk_p50 > 1.6:
            risk -= 0.15
    risk = float(np.clip(risk, 0.0, 1.0))
    level = "низкий" if risk < 0.33 else ("умеренный" if risk < 0.45 else ("повышенный" if risk < 0.58 else "высокий"))
    out = {
        "risk_level": level,
        "p_below_norm": round(p_dry, 3),
        "gtk_class": gtk_class(gtk_p50),
        "spi_p50": spi_p50,
        "deficit_mm": round(deficit, 1) if deficit is not None else None,
    }
    if deficit is not None and deficit > 20 and risk >= 0.45:
        out["irrigation_hint_m3_ha"] = units.mm_to_m3_ha(deficit)
        out["irrigation_basis"] = "дефицит осадков относительно нормы (tp p50); не водобалансовый расчёт ETc"
    return out


def sowing_advice(months, t2m_q, tp_q):
    ms = [int(str(m).split("-")[1]) for m in months]
    tips = []
    for crop in CROPS:
        a, b = crop["sow_months"]
        if not any(m in (a, b, b + 1 if b < 12 else 1) for m in ms):
            continue
        t50 = t2m_q.get("p50")
        if t50 is None:
            continue
        lo, hi = crop["t_window"]
        if t50 < lo - 2.0:
            verdict = "холоднее окна сева: риск затянутых всходов, смещать к концу окна"
        elif t50 > hi + 2.0:
            verdict = "теплее окна сева: ранний сев повышает риск вредителей и влагостресса"
        else:
            verdict = "температурный фон в пределах окна сева"
        wet = tp_q.get("p90", 0.0) - tp_q.get("p10", 0.0)
        tips.append(
            {
                "crop": crop["name"],
                "sow_window_months": [a, b],
                "t2m_p50_c": round(float(t50), 1),
                "verdict": verdict,
                "precip_spread_mm": round(float(wet), 1),
            }
        )
    return tips


def harvest_advice(months, tp_probs, tp_q_mm):
    ms = [int(str(m).split("-")[1]) for m in months]
    if not (7 in ms or 8 in ms or 9 in ms or 10 in ms):
        return None
    p_wet = float(tp_probs.get("above", 1.0 / 3.0))
    if p_wet >= 0.45:
        return "повышенный риск увлажнения в уборку: планировать ранние окна и резерв сушилки"
    if p_wet <= 0.28:
        return "осадки скорее ниже нормы: благоприятный фон уборки и глубокой вспашки"
    return "осадки около нормы: стандартный план уборки"


def livestock_stress(months, t2m_q):
    ms = [int(str(m).split("-")[1]) for m in months]
    if not any(m in (6, 7, 8) for m in ms):
        return None
    p90 = t2m_q.get("p90")
    if p90 is None:
        return None
    level = "умеренный" if p90 < HEAT_T - 2 else ("высокий" if p90 < HEAT_T + 2 else "экстремальный")
    return f"летний тепловой стресс скота ({level}): t2m p90 {p90:.1f}°C — вентиляция, водопой, смещение кормлений"

def season_advice(entry, gtk_p50=None, spi_p50=None, tp_norm_mm=None):
    t2m = entry.get("t2m", {})
    tp = entry.get("tp", {})
    if "tercile_probs" not in tp or "quantiles_mm" not in tp:
        return None
    months = entry.get("months", [])
    ms = [int(str(m).split("-")[1]) for m in months] if months else [entry.get("month", 1)]
    warm = sum(1 for m in ms if 4 <= m <= 9)
    if warm < 2:
        gtk_p50 = None
    out = {
        "drought": drought_block(tp["tercile_probs"], tp["quantiles_mm"], tp_norm_mm, gtk_p50, spi_p50),
        "sowing": sowing_advice(months, t2m.get("quantiles_c", {}), tp["quantiles_mm"]),
        "harvest": harvest_advice(months, tp["tercile_probs"], tp["quantiles_mm"]),
        "livestock": livestock_stress(months, t2m.get("quantiles_c", {})),
        "disclaimer": "ориентиры по терцилям/квантилям прогноза; не заменяют агронома",
    }
    return out
