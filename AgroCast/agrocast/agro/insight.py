import numpy as np
import pandas as pd

WILTING = 0.10
SOIL_DEPTH_MM = 2000.0
KC_GENERIC = 0.85

CROPS = [
    {"key": "winter_wheat", "name": "Озимая пшеница", "base": 5, "need": 1600, "months": [4, 5, 6, 7],
     "kc": {3: 0.4, 4: 0.8, 5: 1.15, 6: 1.0, 7: 0.6},
     "note": "от возобновления вегетации до уборки"},
    {"key": "winter_barley", "name": "Озимый ячмень", "base": 5, "need": 1400, "months": [4, 5, 6],
     "kc": {3: 0.4, 4: 0.85, 5: 1.1, 6: 0.7}, "note": "уборка раньше пшеницы на 10-14 дней"},
    {"key": "sunflower", "name": "Подсолнечник", "base": 10, "need": 2200, "months": [5, 6, 7, 8, 9],
     "kc": {5: 0.5, 6: 0.8, 7: 1.1, 8: 1.0, 9: 0.6}, "note": "вегетация от всходов до уборки"},
    {"key": "maize", "name": "Кукуруза на зерно", "base": 10, "need": 2500, "months": [5, 6, 7, 8, 9],
     "kc": {5: 0.5, 6: 0.9, 7: 1.2, 8: 1.0, 9: 0.6}, "note": "раннеспелые гибриды от 2200"},
    {"key": "soy", "name": "Соя", "base": 10, "need": 2300, "months": [6, 7, 8, 9],
     "kc": {6: 0.7, 7: 1.1, 8: 1.1, 9: 0.7}, "note": "культура короткого дня"},
    {"key": "rapeseed", "name": "Озимый рапс", "base": 5, "need": 1700, "months": [4, 5, 6],
     "kc": {4: 0.7, 5: 1.0, 6: 0.7}, "note": "уборка в конце июня – июле"},
    {"key": "sugar_beet", "name": "Сахарная свёкла", "base": 10, "need": 2700, "months": [5, 6, 7, 8, 9],
     "kc": {5: 0.5, 6: 0.8, 7: 1.15, 8: 1.15, 9: 0.9}, "note": "максимальная потребность во влаге летом"},
]

NOTABLE_YEARS = {
    2007: "аномально тёплая зима в России",
    2010: "рекордная жара и засуха европейской части, катастрофические потери урожая",
    2012: "засушливый год в Южном и Северо-Кавказском округах",
    2015: "мягкая зима, ранняя вегетация",
    2020: "очень тёплая зима 2019/20, рекорды декабря–февраля",
    2022: "тёплый год, дефицит осадков в начале лета на юге",
}


def ra_mm_day(lat, doy):
    phi = np.radians(lat)
    dr = 1.0 + 0.033 * np.cos(2.0 * np.pi * doy / 365.0)
    dec = 0.409 * np.sin(2.0 * np.pi * doy / 365.0 - 1.39)
    x = np.clip(-np.tan(phi) * np.tan(dec), -1.0, 1.0)
    ws = np.arccos(x)
    ra = (24.0 * 60.0 / np.pi) * 0.0820 * dr * (ws * np.sin(phi) * np.sin(dec) + np.cos(phi) * np.cos(dec) * np.sin(ws))
    return ra / 2.45


def et0_hargreaves(tmin, tmax, tmean, lat, doys):
    tmin = np.asarray(tmin, float)
    tmax = np.asarray(tmax, float)
    tmean = np.asarray(tmean, float)
    ra = np.array([ra_mm_day(lat, int(d)) for d in doys])
    e = 0.0023 * ra * (tmean + 17.8) * np.clip(tmax - tmin, 0.5, None) ** 0.5
    return np.clip(e, 0.0, None)


def _decade_splits(df):
    out = []
    for p, g in df.groupby(pd.Grouper(freq="MS")):
        n = len(g)
        bounds = [(0, min(10, n)), (min(10, n), min(20, n)), (min(20, n), n)]
        for a, b in bounds:
            if b > a:
                out.append((p, a, g.iloc[a:b]))
    return out


def _climate_norm(monthly, var, months, y0=1991, y1=2020):
    s = monthly[var].dropna()
    s = s[(s.index.year >= y0) & (s.index.year <= y1)]
    return {m: float(s[s.index.month == m].mean()) for m in months if (s.index.month == m).any()}


def season_insight(members, lat, monthly, swvl_last):
    if not members:
        return None
    df0 = members[0]
    months = sorted({d.month for d in df0.index})
    norms_t = _climate_norm(monthly, "t2m", months)
    norms_p = _climate_norm(monthly, "tp", months)
    period_months = pd.period_range(df0.index[0], df0.index[-1], freq="M")

    dec_stats = {}
    for df in members:
        for p, a, g in _decade_splits(df):
            key = f"{p.year}-{p.month:02d}-{a // 10 + 1}"
            t = g["t2m"].to_numpy(float)
            tmin = g["tmin"].to_numpy(float) if "tmin" in g else t - 4.0
            tmax = g["tmax"].to_numpy(float) if "tmax" in g else t + 4.0
            pr = g["tp"].to_numpy(float)
            doys = [d.dayofyear for d in g.index]
            e = et0_hargreaves(tmin, tmax, t, lat, doys)
            anom = float(np.mean(t) - norms_t.get(p.month, np.nan))
            st = dec_stats.setdefault(key, {"t_anom": [], "pr": [], "et0": [], "tmin_min": [], "pr_max": [], "days_wet10": []})
            st["t_anom"].append(anom)
            st["pr"].append(float(pr.sum()))
            st["et0"].append(float(e.sum()))
            st["tmin_min"].append(float(tmin.min()))
            st["pr_max"].append(float(pr.max()))
            st["days_wet10"].append(int((pr > 10.0).sum()))
    decades = []
    for key in sorted(dec_stats):
        st = dec_stats[key]
        anom = np.array(st["t_anom"])
        pr = np.array(st["pr"])
        et0 = np.array(st["et0"])
        tmin = np.array(st["tmin_min"])
        days_wet10 = np.array(st["days_wet10"], float)
        field_ok = float((days_wet10 <= 1).mean())
        decades.append(
            {
                "label": key,
                "t_anom": {"p10": round(float(np.quantile(anom, 0.1)), 1), "p50": round(float(np.quantile(anom, 0.5)), 1), "p90": round(float(np.quantile(anom, 0.9)), 1)},
                "precip_mm": {"p10": round(float(np.quantile(pr, 0.1))), "p50": round(float(np.quantile(pr, 0.5))), "p90": round(float(np.quantile(pr, 0.9)))},
                "et0_mm": round(float(np.quantile(et0, 0.5))),
                "tmin_min_p10": round(float(np.quantile(tmin, 0.1)), 1),
                "field_ok": round(field_ok, 2),
            }
        )

    sat5, sat10, tot_pr, tot_et0 = [], [], [], []
    heat33, winter_thaw = [], []
    for df in members:
        t = df["t2m"].to_numpy(float)
        tmin = df["tmin"].to_numpy(float) if "tmin" in df else t - 4.0
        tmax = df["tmax"].to_numpy(float) if "tmax" in df else t + 4.0
        pr = df["tp"].to_numpy(float)
        sat5.append(float(np.clip(t - 5.0, 0, None).sum()))
        sat10.append(float(np.clip(t - 10.0, 0, None).sum()))
        doys = [d.dayofyear for d in df.index]
        tot_et0.append(float(et0_hargreaves(tmin, tmax, t, lat, doys).sum()))
        tot_pr.append(float(pr.sum()))
        heat33.append(int((tmax > 33.0).sum()))
        thaw = 0
        for d, tm in zip(df.index, t):
            if tm > 3.0 and d.month in (12, 1, 2):
                thaw += 1
        winter_thaw.append(thaw)

    def q(x):
        x = np.array(x, float)
        return {"p10": round(float(np.quantile(x, 0.1))), "p50": round(float(np.quantile(x, 0.5))), "p90": round(float(np.quantile(x, 0.9)))}

    sat = {"b5": q(sat5), "b10": q(sat10), "crops": []}
    for c in CROPS:
        overlap = len(set(c["months"]) & set(months))
        if overlap == 0:
            continue
        src = sat5 if c["base"] == 5 else sat10
        cov = float(np.median(src)) / c["need"] * 100.0
        if overlap >= 3:
            verdict = "дефицит тепла" if cov < 85 else ("достаточно тепла" if cov <= 115 else "избыток жары")
        else:
            verdict = f"период покрывает {overlap} из {len(c['months'])} мес цикла — это САТ только за период прогноза"
        sat["crops"].append({"name": c["name"], "base": c["base"], "need": c["need"], "coverage": round(cov), "verdict": verdict, "note": c["note"]})

    reserve_mm = None
    if swvl_last is not None and np.isfinite(swvl_last):
        reserve_mm = round(max(0.0, (float(swvl_last) - WILTING) * SOIL_DEPTH_MM))
    kc = KC_GENERIC
    for c in CROPS:
        if set(c["months"]) & set(months):
            kcs = [c["kc"].get(m) for m in months if c["kc"].get(m)]
            if kcs:
                kc = float(np.mean(kcs))
            break
    pr50 = float(np.median(tot_pr))
    etc50 = float(np.median(tot_et0)) * kc
    deficit = etc50 - pr50
    usable = 0.4 * (reserve_mm or 0.0)
    irrig = max(0.0, deficit - usable)
    water = {
        "reserve_mm": reserve_mm,
        "swvl": round(float(swvl_last), 3) if swvl_last is not None else None,
        "kc": round(kc, 2),
        "et0_mm": q(tot_et0),
        "etc_mm": round(etc50),
        "precip_mm": q(tot_pr),
        "deficit_mm": round(deficit),
        "irrigation_m3_ha": {"p10": round(max(0.0, (np.quantile(tot_et0, 0.9) * kc - np.quantile(tot_pr, 0.1)) * 10 - usable)),
                              "p50": round(irrig * 10)},
    }

    for d, key in zip(decades, sorted(dec_stats)):
        st = dec_stats[key]
        need = np.array(st["et0"], float) * kc - np.array(st["pr"], float)
        d50 = max(0.0, float(np.quantile(need, 0.5)))
        ddry = max(0.0, float(np.quantile(st["et0"], 0.9) * kc - np.quantile(st["pr"], 0.1)))
        d["irr_m3_ha"] = {"p50": round(d50 * 10), "p10": round(ddry * 10)}

    risks = []

    def prob(cond):
        return round(float(np.mean(cond)), 2)

    frost_any, frost_dates, heat_hits, rain_hits, thaw_hits, bare_frost_hits = [], [], [], [], [], []
    for df in members:
        t = df["t2m"].to_numpy(float)
        tmin = df["tmin"].to_numpy(float) if "tmin" in df else t - 4.0
        tmax = df["tmax"].to_numpy(float) if "tmax" in df else t + 4.0
        pr = df["tp"].to_numpy(float)
        spr = [d for d, m in zip(df.index, tmin) if m < 0.0 and 4 <= d.month <= 6]
        frost_any.append(bool(spr))
        if spr:
            frost_dates.append(spr[-1].strftime("%d.%m"))
        heat_hits.append(bool(np.any([(d.month in (6, 7)) and (x > 33.0) for d, x in zip(df.index, tmax)])))
        rain_hits.append(int(np.sum([(d.month in (7, 8)) and (x > 15.0) for d, x in zip(df.index, pr)]) >= 2))
        thaw_hits.append(int(np.sum([(d.month in (12, 1, 2)) and (x > 3.0) for d, x in zip(df.index, t)]) >= 5))
        bare_frost_hits.append(bool(np.any([(d.month in (12, 1, 2)) and (x < -18.0) for d, x in zip(df.index, tmin)])))
    if set(months) & {4, 5, 6}:
        p_frost = prob(frost_any)
        extra = ""
        if frost_dates:
            ds = sorted(frost_dates)
            extra = f"; последний заморозок (медиана): {ds[len(ds) // 2]}"
        risks.append({"key": "frost_spring", "label": "Весенние заморозки (tmin < 0°C)", "window": "апрель–июнь" + extra,
                      "prob": p_frost, "impact": "повреждение цветущих садов, озимых, всходов",
                      "action": "задержать ранние обработки, дымовые шашки/сетка наготове, сев после окна риска"})
    if set(months) & {6, 7} and prob(heat_hits) > 0:
        risks.append({"key": "heat33", "label": "Жара >33°C в налив (июнь–июль)", "window": "15 июня – 20 июля",
                      "prob": prob(heat_hits), "impact": "щуплость зерна, снижение масличности подсолнечника",
                      "action": "не затягивать уборку, антистрессовые обработки (аминокислоты, кремний)"})
    if set(months) & {7, 8} and prob(rain_hits) > 0:
        risks.append({"key": "harvest_rain", "label": "Ливни в уборку (≥2 дня >15 мм)", "window": "июль–август",
                      "prob": prob(rain_hits), "impact": "полегание, прорастание зерна в валках, грязь на току",
                      "action": "резерв сушилки, ранние окна прямого комбайнирования, порядок полей от дальних"})
    if set(months) & {12, 1, 2} and prob(thaw_hits) > 0:
        risks.append({"key": "winter_thaw", "label": "Оттепели (≥5 дней >3°C)", "window": "декабрь–февраль",
                      "prob": prob(thaw_hits), "impact": "истощение и выпревание озимых, ледяная корка после возврата мороза, гибель посевов на 10–40%",
                      "action": "заранее выбрать устойчивые сорта, весной — азот + ретардант по ослабленным полям, оценка стояния после схода снега"})
    if set(months) & {12, 1, 2} and prob(bare_frost_hits) > 0:
        risks.append({"key": "bare_frost", "label": "Мороз без снежного укрытия (tmin < −18°C)", "window": "декабрь–февраль",
                      "prob": prob(bare_frost_hits), "impact": "вымерзание узла кущения озимых на бесснежных полях; под снегом 10+ см озимые переносят такие морозы",
                      "action": "зимой не лечится — страховка и зимостойкие сорта решали вопрос осенью; весной оценка стояния и пересев погибших участков"})
    return {
        "decades": decades,
        "water": water,
        "sat": sat,
        "risks": risks,
        "heat_days_33": q(heat33),
        "winter_thaw_days": q(winter_thaw),
        "months": [str(p) for p in period_months],
    }


GRAIN_HARVEST = {2008: 108, 2009: 97, 2010: 61, 2011: 94, 2012: 71, 2013: 92, 2014: 105, 2015: 105, 2016: 121, 2017: 136, 2018: 113, 2019: 121, 2020: 134, 2021: 121, 2022: 158, 2023: 145, 2024: 125}
GRAIN_NOTE = {2010: "сильная засуха — минимум сбора с 2000-х", 2012: "засуха Юга и Сибири", 2016: "рекорд на тот момент", 2022: "исторический рекорд"}


def analogs_facts(analog_years, monthly, season_months, season_year):
    out = []
    s_t = monthly["t2m"].dropna()
    s_p = monthly["tp"].dropna()
    for a in analog_years[:5]:
        y = int(a["year"])
        span = pd.period_range(pd.Period(f"{y}-{season_months[0]:02d}", "M"), periods=len(season_months), freq="M")
        t_vals = np.array([float(s_t.get(p, np.nan)) for p in span])
        p_vals = np.array([float(s_p.get(p, np.nan)) for p in span])
        if np.isnan(t_vals).all():
            continue
        norms_t = np.nanmean([s_t[(s_t.index.month == p.month) & (s_t.index.year >= 1991) & (s_t.index.year <= 2020)].mean() for p in span])
        norms_p = np.nanmean([s_p[(s_p.index.month == p.month) & (s_p.index.year >= 1991) & (s_p.index.year <= 2020)].mean() for p in span])
        t_anom = float(np.nanmean(t_vals) - norms_t)
        p_pct = float(np.nanmean(p_vals) / norms_p * 100.0) if norms_p > 0 else None
        gh = GRAIN_HARVEST.get(y)
        ynote = None
        if gh is not None:
            ynote = f"зерно в РФ: ≈{gh} млн т" + (f" — {GRAIN_NOTE[y]}" if y in GRAIN_NOTE else "")
        out.append({"year": y, "weight": round(float(a.get("weight", 0)), 3),
                    "t_anom": round(t_anom, 1), "tp_pct_of_norm": round(p_pct) if p_pct is not None else None,
                    "note": NOTABLE_YEARS.get(y), "yield_note": ynote})
    return out


def passport(skill_df, records_df, variable, target_months, mode):
    if skill_df is None or records_df is None:
        return None
    s = skill_df[(skill_df.variable == variable) & (skill_df.target_month.isin(target_months))]
    if s.empty:
        return None
    r = records_df[(records_df.variable == variable) & (records_df.target_month.isin(target_months))]
    if r.empty:
        return None
    P = r[["p0", "p1", "p2"]].to_numpy(float)
    dom = P.argmax(axis=1)
    pmax = P.max(axis=1)
    obs = r.obs_tercile.to_numpy(int)
    conf = pmax >= 0.40
    return {
        "rpss": round(float(np.average(s.rpss, weights=s.n)), 3),
        "n_years": int(s.n.sum()),
        "hit_all": round(float((dom == obs).mean()), 2),
        "hit_conf": round(float((dom[conf] == obs[conf]).mean()), 2) if conf.any() else None,
        "coverage_conf": round(float(conf.mean()), 2),
    }
