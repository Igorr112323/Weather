ECON = {
    "winter_wheat": {"price": 12000, "yield": 6.0, "loss_drought": 0.25, "loss_heat": 0.15, "cost_irr": 4000, "cost_anti": 900},
    "winter_barley": {"price": 11600, "yield": 5.5, "loss_drought": 0.25, "loss_heat": 0.15, "cost_irr": 4000, "cost_anti": 900},
    "sunflower": {"price": 37000, "yield": 2.5, "loss_drought": 0.30, "loss_heat": 0.20, "cost_irr": 4500, "cost_anti": 1100},
    "maize": {"price": 11900, "yield": 7.0, "loss_drought": 0.25, "loss_heat": 0.20, "cost_irr": 4500, "cost_anti": 1100},
    "soy": {"price": 32000, "yield": 2.0, "loss_drought": 0.30, "loss_heat": 0.15, "cost_irr": 4500, "cost_anti": 1100},
    "rapeseed": {"price": 27000, "yield": 2.5, "loss_drought": 0.25, "loss_heat": 0.20, "cost_irr": 4000, "cost_anti": 1000},
    "sugar_beet": {"price": 3000, "yield": 45.0, "loss_drought": 0.30, "loss_heat": 0.10, "cost_irr": 5500, "cost_anti": 1200},
}

SOURCE = "цены производителей — Росстат (январь 2026: пшеница 12,0; ячмень 11,6; кукуруза 11,9 тыс. ₽/т; подсолнечник ~37 тыс. ₽/т), соя/рапс/свёкла — рыночный ориентир; урожайность типовая для юга РФ; ориентир для расчёта, не биржевая котировка"

NAMES = {
    "winter_wheat": "Озимая пшеница",
    "winter_barley": "Озимый ячмень",
    "sunflower": "Подсолнечник",
    "maize": "Кукуруза на зерно",
    "soy": "Соя",
    "rapeseed": "Озимый рапс",
    "sugar_beet": "Сахарная свёкла",
}


def _verdict(loss, cost):
    if loss >= cost:
        return "окупается"
    if loss >= 0.7 * cost:
        return "на грани"
    return "не окупается"


def econ_block(ph, drought_p=None, heat_p=None):
    rows = []
    ph_crops = {c.get("key"): c for c in ph.get("crops", []) if c.get("key")}
    for key, e in ECON.items():
        c = ph_crops.get(key)
        dr = None
        ht = None
        if c:
            for s in c.get("stages", []):
                if s.get("crit") == "засуха":
                    dr = max(dr or 0.0, s["prob"])
                if s.get("crit") == "жара >33°C":
                    ht = max(ht or 0.0, s["prob"])
        if dr is None:
            dr = float(drought_p or 0.0)
        if ht is None:
            ht = float(heat_p or 0.0)
        loss_dr = dr * e["loss_drought"] * e["yield"] * e["price"]
        loss_ht = ht * e["loss_heat"] * e["yield"] * e["price"]
        rows.append(
            {
                "key": key,
                "name": NAMES[key],
                "price_rub_t": e["price"],
                "yield_t_ha": e["yield"],
                "drought_p": round(dr, 2),
                "heat_p": round(ht, 2),
                "loss_drought_rub": round(loss_dr / 10) * 10,
                "loss_heat_rub": round(loss_ht / 10) * 10,
                "cost_irr_rub": e["cost_irr"],
                "cost_anti_rub": e["cost_anti"],
                "irrigation": _verdict(loss_dr, e["cost_irr"]),
                "antistress": _verdict(loss_ht, e["cost_anti"]),
                "risk_rub_ha": round(max(loss_dr, loss_ht) / 10) * 10,
            }
        )
    return {"crops": rows, "source": SOURCE}
