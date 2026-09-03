import numpy as np
import pandas as pd

MNS = ["", "янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]
ROM = ["I", "II", "III"]

CROP_LABELS = {
    "winter_wheat": "Озимая пшеница",
    "winter_barley": "Озимый ячмень",
    "sunflower": "Подсолнечник",
    "maize": "Кукуруза на зерно",
    "soy": "Соя",
    "rapeseed": "Озимый рапс",
    "sugar_beet": "Сахарная свёкла",
}

SOW_MIN_T = {"sunflower": 12.0, "maize": 10.0, "soy": 12.0, "sugar_beet": 6.0, "winter_wheat": 14.0, "winter_barley": 14.0}

STAGES = {
    "winter_wheat": [
        {"name": "Возобновление вегетации", "w": (3, 2, 4, 1), "crit": "frost", "why": "заморозок повреждает отросшие листья и узел кущения", "act": "азотная подкормка до похолодания, контроль зимующих вредителей"},
        {"name": "Выход в трубку", "w": (4, 3, 5, 2), "crit": "frost", "why": "заморозок −5°C убивает зачаточный колос", "act": "отложить гербициды перед волной холода, готовить фолиар-подкормку"},
        {"name": "Колошение", "w": (5, 3, 6, 1), "crit": "heat", "why": "жара >33°C стерилизует пыльцу", "act": "фунгицид + антистресс до колошения, не затягивать"},
        {"name": "Налив зерна", "w": (6, 2, 6, 3), "crit": "heat", "why": "жара в налив → щуплое зерно, минус 3–7 ц/га", "act": "ранняя уборочная готовность, подбор гибридов учтён на следующий год"},
        {"name": "Уборка", "w": (7, 1, 7, 3), "crit": "rain", "why": "ливни → полегание и прорастание зерна в валках", "act": "прямое комбайнирование в первых окнах, резерв сушилки"},
    ],
    "winter_barley": [
        {"name": "Возобновление вегетации", "w": (3, 2, 4, 1), "crit": "frost", "why": "ячмень чувствительнее пшеницы к возврату холодов", "act": "ранняя азотная подкормка после схода снега"},
        {"name": "Колошение", "w": (5, 2, 5, 3), "crit": "frost", "why": "заморозок в колошение — потеря колоса", "act": "контроль прогноза перед обработками"},
        {"name": "Налив зерна", "w": (6, 1, 6, 2), "crit": "heat", "why": "жара сжимает налив", "act": "уборка сразу при восковой спелости"},
        {"name": "Уборка", "w": (6, 2, 7, 1), "crit": "rain", "why": "дожди → полегание и потеря качества пивоваренного зерна", "act": "окна прямого комбайнирования"},
    ],
    "sunflower": [
        {"name": "Сев", "w": (4, 3, 5, 2), "crit": "sow", "why": "почва должна прогреться до 12°C и быть сухой для проезда", "act": "сев в окно без ливней"},
        {"name": "Всходы", "w": (5, 2, 5, 3), "crit": "frost", "why": "заморозок −3…−4°C губит всходы", "act": "не спешить с ранним севом в морозоопасных балках"},
        {"name": "Цветение", "w": (7, 1, 7, 3), "crit": "heat", "why": "жара >33°C пустоцвет, минус масличность", "act": "антистресс (бор, аминокислоты) перед цветением"},
        {"name": "Налив", "w": (7, 3, 8, 2), "crit": "heat", "why": "жара + дефицит влаги → щуплость", "act": "десикация по готовности, не ждать полной спелости"},
        {"name": "Уборка", "w": (9, 2, 10, 1), "crit": "rain", "why": "ливни сбивают корзинки и размывают валки", "act": "порядок полей от дальних, резерв сушилки"},
    ],
    "maize": [
        {"name": "Сев", "w": (4, 3, 5, 2), "crit": "sow", "why": "почва ≥10°C, проезд техники", "act": "сев в окно без ливней"},
        {"name": "Всходы", "w": (5, 2, 6, 1), "crit": "frost", "why": "заморозок губит всходы", "act": "учёт морозобалок при разбиении полей"},
        {"name": "Цветение (метёлка)", "w": (7, 2, 7, 3), "crit": "heat", "why": "жара >33°C в цветение — недоопыление, «пропуски» в початке", "act": "антистресс до цветения, выбор гибридов учтён"},
        {"name": "Восковая спелость", "w": (8, 1, 8, 3), "crit": "drought", "why": "дефицит влаги сжимает налив зерна", "act": "контроль влажности зерна перед уборкой"},
        {"name": "Уборка", "w": (9, 2, 10, 1), "crit": "rain", "why": "дожди → грязь, потери початков", "act": "сушка зерна, порядок полей"},
    ],
    "soy": [
        {"name": "Сев", "w": (5, 1, 5, 2), "crit": "sow", "why": "почва ≥12°C", "act": "сев после прогрева почвы"},
        {"name": "Всходы", "w": (5, 3, 6, 1), "crit": "frost", "why": "заморозок губит всходы", "act": "не сеять раньше оптимального окна"},
        {"name": "Цветение", "w": (7, 1, 7, 3), "crit": "heat", "why": "жара сбивает завязывание бобов", "act": "антистресс, орошение при возможности"},
        {"name": "Налив бобов", "w": (8, 1, 8, 3), "crit": "drought", "why": "дефицит влаги → мелкое зерно", "act": "полив по декадному балансу"},
        {"name": "Уборка", "w": (9, 3, 10, 2), "crit": "rain", "why": "дожди → растрескивание бобов и потери", "act": "своевременная раздельная уборка"},
    ],
    "rapeseed": [
        {"name": "Возобновление вегетации", "w": (3, 2, 4, 1), "crit": "frost", "why": "поздние морозы после отрастания", "act": "азот + сера ранней весной, инсектицид от рапсового цветоеда"},
        {"name": "Цветение", "w": (4, 2, 5, 1), "crit": "frost", "why": "заморозок в цветение — потеря стручков", "act": "обработки вне похолоданий"},
        {"name": "Налив", "w": (5, 2, 6, 1), "crit": "heat", "why": "жара + засуха → низкая масличность", "act": "бор в фазе бутонизации учтён заранее"},
        {"name": "Уборка", "w": (6, 2, 7, 1), "crit": "rain", "why": "дожди → осыпание и прорастание в валках", "act": "десикация и ранние окна"},
    ],
    "sugar_beet": [
        {"name": "Сев", "w": (4, 1, 4, 3), "crit": "sow", "why": "почва ≥6°C, сухое окно", "act": "сев в первые пригодные декады"},
        {"name": "Всходы", "w": (4, 3, 5, 2), "crit": "frost", "why": "заморозок губит всходы", "act": "учёт морозоопасных полей"},
        {"name": "Смыкание рядков", "w": (6, 1, 6, 3), "crit": "drought", "why": "дефицит влаги тормозит листовой аппарат", "act": "полив при орошении, сохранение влаги"},
        {"name": "Набор массы", "w": (7, 1, 8, 3), "crit": "drought", "why": "засуха напрямую режет урожай корней и сахаристость", "act": "полив по балансу, листовые подкормки"},
        {"name": "Уборка", "w": (9, 3, 10, 2), "crit": "rain", "why": "дожди → грязь, потери при выкопке", "act": "график копки с запасом окон"},
    ],
}

CRIT_LABEL = {"frost": "заморозок", "heat": "жара >33°C", "drought": "засуха", "rain": "ливни", "sow": "окно сева"}


def _win_label(w):
    m0, d0, m1, d1 = w
    a = f"{MNS[m0]} {ROM[d0 - 1]}"
    b = f"{MNS[m1]} {ROM[d1 - 1]}"
    return a if (m0, d0) == (m1, d1) else f"{a} – {b}"


def _win_mask(idx, w):
    m0, d0, m1, d1 = w
    s = (idx.month > m0) | ((idx.month == m0) & (idx.day > (d0 - 1) * 10))
    e = (idx.month < m1) | ((idx.month == m1) & (idx.day <= d1 * 10))
    return s & e


def _gtk(df):
    t = df["t2m"].to_numpy(float)
    pr = df["tp"].to_numpy(float)
    warm = t > 10.0
    den = 0.1 * t[warm].sum()
    return float(pr[warm].sum() / den) if den > 1e-9 else np.nan


def _stage_stats(members, w, crit, crop):
    hits = []
    soil_t = []
    for df in members:
        idx = df.index
        m = _win_mask(idx, w)
        if not m.any():
            continue
        g = df.loc[m]
        t = g["t2m"].to_numpy(float)
        tmin = g["tmin"].to_numpy(float) if "tmin" in g else t - 4.0
        tmax = g["tmax"].to_numpy(float) if "tmax" in g else t + 4.0
        pr = g["tp"].to_numpy(float)
        soil_t.append(float(np.mean(t)))
        if crit == "frost":
            hits.append(bool((tmin < 0.0).any()))
        elif crit == "heat":
            hits.append(bool((tmax > 33.0).any()))
        elif crit == "rain":
            hits.append(bool((pr > 15.0).sum() >= 2))
        elif crit == "drought":
            gk = _gtk(df)
            hits.append(bool(np.isfinite(gk) and gk < 0.9))
        elif crit == "sow":
            ok_rain = not (pr > 10.0).any()
            ok_t = float(np.mean(t)) >= SOW_MIN_T.get(crop, 10.0)
            hits.append(bool(ok_rain and ok_t))
    if not hits:
        return None
    return {
        "prob": round(float(np.mean(hits)), 2),
        "n": len(hits),
        "soil_t": round(float(np.median(soil_t)), 1) if soil_t else None,
    }


def phenology_block(members):
    if not members:
        return None
    idx0 = members[0].index
    months = sorted({d.month for d in idx0})
    years = sorted({d.year for d in idx0})
    crops = []
    for key, stages in STAGES.items():
        if key != "maize":
            continue
        rows = []
        probs = []
        for st in stages:
            w = st["w"]
            in_period = set(range(w[0], w[2] + 1)) & set(months)
            if not in_period:
                continue
            s = _stage_stats(members, w, st["crit"], key)
            if s is None:
                continue
            row = {
                "name": st["name"],
                "window": _win_label(w),
                "crit": CRIT_LABEL[st["crit"]],
                "prob": s["prob"],
                "impact": st["why"],
                "action": st["act"],
            }
            if st["crit"] == "sow":
                row["soil_t"] = s["soil_t"]
                row["need_t"] = SOW_MIN_T.get(key)
            else:
                probs.append(s["prob"])
            rows.append(row)
        if not rows:
            continue
        p_stress = max(probs) if probs else None
        if p_stress is None:
            verdict = "окно сева: смотрите пригодность декад"
        elif p_stress < 0.25:
            verdict = "благоприятно"
        elif p_stress < 0.5:
            verdict = "умеренный риск"
        else:
            verdict = "неблагоприятно"
        crops.append({"key": key, "name": CROP_LABELS[key], "verdict": verdict, "p_stress": p_stress, "stages": rows})
    drought_p = None
    gtks = [_gtk(df) for df in members]
    gtks = [g for g in gtks if np.isfinite(g)]
    if gtks:
        drought_p = round(float(np.mean([g < 0.8 for g in gtks])), 2)
    return {"crops": crops, "drought_p": drought_p, "months": months, "years": years}
