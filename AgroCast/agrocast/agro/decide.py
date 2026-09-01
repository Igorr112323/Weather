DECISIONS = [
    {"key": "irrigate", "label": "Полив (включая влагозарядку)", "ratio": 0.30, "note": "затраты полива ≈30% от потерь урожая при засухе"},
    {"key": "antistress", "label": "Антистресс-обработки (аминокислоты, кремний)", "ratio": 0.15, "note": "дешёвая страховка от жары, окупается чаще"},
    {"key": "insure", "label": "Страхование от засухи (с господдержкой)", "ratio": 0.10, "note": "взнос ≈10% страховой суммы"},
]


def decision_table(drought_p, heat_p):
    rows = []
    for d in DECISIONS:
        p = None
        if d["key"] == "irrigate":
            p = drought_p
        elif d["key"] == "antistress":
            p = heat_p
        elif d["key"] == "insure":
            p = drought_p
        if p is None:
            continue
        if p > d["ratio"] + 0.07:
            verdict = "действовать"
        elif p < d["ratio"] - 0.07:
            verdict = "не окупается"
        else:
            verdict = "на грани — решать вам"
        rows.append({"label": d["label"], "prob": round(float(p), 2), "ratio": d["ratio"], "verdict": verdict, "note": d["note"]})
    return rows
