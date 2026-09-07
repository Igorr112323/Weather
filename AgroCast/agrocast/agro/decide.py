DECISIONS = [
    {"key": "irrigate", "label": "Полив (включая влагозарядку)", "ratio": 0.30, "note": "затраты полива ≈30% от потерь урожая при засухе"},
    {"key": "antistress", "label": "Антистресс-обработки (аминокислоты, кремний)", "ratio": 0.15, "note": "дешёвая страховка от жары, окупается чаще"},
    {"key": "insure", "label": "Страхование от засухи (с господдержкой)", "ratio": 0.10, "note": "взнос ≈10% страховой суммы"},
]


def decision_table(drought_p, heat_p, evidence=None):
    from agrocast.agro.policy import CONFIRMED, UNAVAILABLE

    rows = []
    for d in DECISIONS:
        p = None
        var = "t2m"
        if d["key"] == "irrigate":
            p = drought_p
            var = "tp"
        elif d["key"] == "antistress":
            p = heat_p
        elif d["key"] == "insure":
            p = drought_p
            var = "tp"
        if p is None:
            continue
        status = ((evidence or {}).get(var) or {}).get("status")
        if p > d["ratio"] + 0.07:
            if status == CONFIRMED:
                verdict = "действовать (навык подтверждён)"
            elif status == UNAVAILABLE:
                verdict = "без предписания: нет данных о подтверждённом навыке"
            else:
                verdict = "решить после проверки состояния поля: вероятность ориентировочная (навык прогноза не подтверждён)"
        elif p < d["ratio"] - 0.07:
            verdict = "не окупается"
        else:
            verdict = "на грани — решать вам"
        rows.append({"key": d["key"], "label": d["label"], "prob": round(float(p), 2), "ratio": d["ratio"], "verdict": verdict,
                     "evidence_variable": var, "note": d["note"]})
    return rows
