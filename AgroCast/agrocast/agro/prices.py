from agrocast.agro import corn as corn_params

ECON = {
    "maize": {"yield": corn_params.CORN["yield_t_ha"], "loss_drought": 0.25, "loss_heat": 0.20, "cost_irr": 4500, "cost_anti": 1100},
}

NAMES = {"maize": "Кукуруза на зерно"}

FALLBACK_PRICE = {
    "rub_per_t": 11900,
    "as_of": "2026-01",
    "source": "нет доступа к биржевым котировкам — опорная цена производителей Росстат (январь 2026), не биржевая",
    "stale": True,
    "stale_days": None,
}


def _verdict(loss, cost, confirmed=False):
    if loss >= cost:
        return "окупается (навык подтверждён)" if confirmed else "окупается (по сценарной оценке)"
    if loss >= 0.7 * cost:
        return "на грани"
    return "не окупается"


RUB_PER_M3 = 3.0


def _source_line(p, yield_t_ha=None, variety_name=None):
    stale = " ВНИМАНИЕ: цена устарела — обновите данные." if p.get("stale") else ""
    var = f" Урожайность — по сорту «{variety_name}» из справочника ({yield_t_ha} т/га)." if variety_name else ""
    return (
        f"Цена кукурузы: {p.get('source', '—')} (дата цены: {p.get('as_of', '—')}){stale}"
        f"{var} Параметры культуры (заморозки, САТ, окно сева) — по источникам: kccc.ru, rosgibrid.ru, "
        f"rosagrochim.ru и справочнику сортов. Стоимость полива ≈{RUB_PER_M3:g} ₽/м³ "
        f"(≈{int(4500 / RUB_PER_M3)} м³/га за полив 150 мм — типовая отраслевая оценка). "
        f"Доли потерь от засухи/жары — типовые отраслевые оценки, уточните под свои затраты. "
        f"Деньги — сценарная оценка, не гарантия дохода."
    )


def econ_block(ph, drought_p=None, heat_p=None, price=None, yield_t_ha=None, variety_name=None, water=None):
    p = price if price else dict(FALLBACK_PRICE)
    price_rub_t = float(p.get("rub_per_t") or FALLBACK_PRICE["rub_per_t"])
    w_irr = (water or {}).get("irrigation_m3_ha") or {}
    irr_m3 = w_irr.get("p50")
    irr_m3_dry = w_irr.get("p10")
    rows = []
    ph_crops = {c.get("key"): c for c in ph.get("crops", []) if c.get("key")} if ph else {}
    for key, e in ECON.items():
        yield_v = float(yield_t_ha) if yield_t_ha else e["yield"]
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
        loss_dr = dr * e["loss_drought"] * yield_v * price_rub_t
        loss_ht = ht * e["loss_heat"] * yield_v * price_rub_t
        rows.append(
            {
                "key": key,
                "name": NAMES[key],
                "variety": variety_name,
                "price_rub_t": round(price_rub_t),
                "price_usd_per_t": p.get("usd_per_t"),
                "price_usd_cents_bushel": p.get("usd_cents_bushel"),
                "price_usd_rub": p.get("usd_rub"),
                "price_as_of": p.get("as_of"),
                "price_contract": p.get("contract"),
                "price_stale": bool(p.get("stale")),
                "yield_t_ha": yield_v,
                "drought_p": round(dr, 2),
                "heat_p": round(ht, 2),
                "loss_drought_rub": round(loss_dr / 10) * 10,
                "loss_heat_rub": round(loss_ht / 10) * 10,
                "cost_irr_rub": e["cost_irr"],
                "cost_anti_rub": e["cost_anti"],
                "irr_m3_ha": irr_m3,
                "irr_m3_ha_p10": irr_m3_dry,
                "irr_cost_rub": round(irr_m3 * RUB_PER_M3) if irr_m3 else None,
                "rub_per_m3": RUB_PER_M3,
                "irrigation": _verdict(loss_dr, e["cost_irr"]),
                "antistress": _verdict(loss_ht, e["cost_anti"]),
                "money_note": "сценарная оценка на вероятностях прогноза и типовых отраслевых затратах; не гарантия дохода",
                "risk_rub_ha": round(max(loss_dr, loss_ht) / 10) * 10,
            }
        )
    return {"crops": rows, "source": _source_line(p, yield_t_ha, variety_name)}
