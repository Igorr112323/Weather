import numpy as np
import pandas as pd


def human_name(col):
    c = col.lower()
    if c.startswith("pc"):
        n = c[2]
        if "_l" in c:
            lag = c.split("_l")[1]
            return f"Океанская траектория PC{n} ({lag} мес назад)"
        if "_s" in c:
            return f"Тенденция океана PC{n} за 3 мес"
        if "_f" in c:
            return f"Прогноз океана (LIM) PC{n} на сезон"
        return f"Океанский сигнал PC{n} (PC1 — ЭНСКО)"
    if c.startswith("med_a"):
        return "Температура Средиземного моря"
    if c.startswith("black_a"):
        return "Температура Чёрного моря"
    if c == "swvl_a":
        return "Запас почвенной влаги"
    if c == "snow_a":
        return "Снежный покров"
    if c == "t2m_a":
        return "Локальное потепление (последние месяцы)"
    if c == "tp_a":
        return "Локальные осадки (последние месяцы)"
    if c == "zpc1_a":
        return "Режим NAO (зональная циркуляция)"
    if c == "zpc2_a":
        return "Восточно-атлантическая мода"
    if c.startswith("zpc"):
        return f"Циркуляция {c[3].upper()}"
    if c.startswith("u10"):
        return "Полярный вихрь (стратосфера, 10 гПа)"
    if c.startswith("z50"):
        return "Высота полярного вихря (50 гПа)"
    if "nino" in c:
        return "ЭНСКО"
    if "nao" in c:
        return "NAO"
    if c == "ao" or c.startswith("ao"):
        return "Арктическая осцилляция"
    return col


def top_drivers(pf, std, season_month, variable, top=3):
    z = std["z"]
    tgt = z[[i for i in z.index if i.month == season_month]]
    issues = [t - 1 for t in tgt.index if (t - 1) in pf.index]
    if len(issues) < 20:
        return []
    y = np.array([float(z.loc[t + 1]) for t in issues])
    rows = []
    last = pf.index[-1]
    for col in pf.columns:
        x = pf.loc[issues, col].to_numpy(float)
        v = pf.loc[last, col]
        if not (np.isfinite(x).all() and np.isfinite(v)):
            continue
        sd = x.std()
        if sd < 1e-6:
            continue
        r = float(np.corrcoef(x, y)[0, 1])
        if not np.isfinite(r):
            continue
        rows.append((col, r, float(v)))
    rows.sort(key=lambda t: -abs(t[1]))
    out = []
    for col, r, v in rows[:top]:
        if variable == "t2m":
            push = "к теплу" if r * v > 0 else "к холоду"
            unit = "°C"
        else:
            push = "к влаге" if r * v > 0 else "к сухости"
            unit = "мм"
        out.append({"name": human_name(col), "r": round(r, 2), "now": round(v, 2), "push": push, "target": unit})
    return out
