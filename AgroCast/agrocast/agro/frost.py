from datetime import date, timedelta

import numpy as np


def _tmin_map(df):
    if "tmin" in list(df.columns):
        t = df["tmin"].to_numpy(float)
    else:
        t = df["t2m"].to_numpy(float) - 4.0
    return dict(zip([d.date() for d in df.index], t))


def _parse_mmdd(s, default):
    try:
        m, d = [int(x) for x in str(s).split("-")]
        return m, d
    except Exception:
        m, d = default
        return m, d


def _sow_date_in(t0, t1, m, d):
    for yr in sorted({t0.year, t1.year}):
        try:
            cand = date(yr, m, d)
        except ValueError:
            continue
        if t0 <= cand <= t1:
            return cand
    return None


def frost_block(members, crop=None):
    if not members:
        return None
    idx0 = [d.date() for d in members[0].index]
    t0, t1 = idx0[0], idx0[-1]
    per = [_tmin_map(df) for df in members]
    spr_any = []
    last_frost = []
    month_hits = {}
    for rec in per:
        spr = [d for d in rec if d.month in (3, 4, 5, 6) and rec[d] < 0.0]
        spr_any.append(bool(spr))
        if spr:
            last_frost.append(max(spr))
        for d, v in rec.items():
            month_hits.setdefault(d.month, []).append(bool(v < 0.0))
    any_frost = [any(rec[d] < 0.0 for d in rec) for rec in per]
    out = {
        "span": [t0.strftime("%d.%m.%Y"), t1.strftime("%d.%m.%Y")],
        "p_frost_any": round(float(np.mean(any_frost)), 2),
        "p_frost_spring": round(float(np.mean(spr_any)), 2),
        "p_frost_day_by_month": {str(int(m)): round(float(np.mean(v)), 3) for m, v in sorted(month_hits.items())},
    }
    if last_frost:
        arr = sorted(last_frost)
        out["last_frost"] = {
            "median": arr[len(arr) // 2].strftime("%d.%m"),
            "p90": arr[max(0, int(np.ceil(0.9 * len(arr))) - 1)].strftime("%d.%m"),
        }
    if not crop:
        return out
    fatal = crop.get("frost_fatal_c")
    fatal = float(fatal) if fatal is not None else -3.0
    sm, sd = _parse_mmdd(crop.get("sow_from"), (4, 20))
    tm_, td_ = _parse_mmdd(crop.get("sow_to"), (5, 15))
    sow_from_d = _sow_date_in(t0, t1, sm, sd)
    row = {
        "name": crop.get("name"),
        "fatal_c": fatal,
        "tol_c": crop.get("frost_tol_c"),
        "sow_from": f"{sm:02d}-{sd:02d}",
        "sow_to": f"{tm_:02d}-{td_:02d}",
    }
    out["crop"] = row
    if sow_from_d is None:
        row["verdict"] = "Период сева сорта вне горизонта прогноза — ориентируйтесь на окно сева из справочника"
        return out
    try:
        sow_to_d = date(sow_from_d.year, tm_, td_)
    except ValueError:
        sow_to_d = date(sow_from_d.year, tm_, min(td_, 28))
    if sow_to_d < sow_from_d:
        sow_to_d += timedelta(days=31)
    HORIZON = 30
    start = max(t0, sow_from_d - timedelta(days=15))
    end = min(t1, sow_to_d + timedelta(days=45))
    dangers = []
    d = start
    while d <= end:
        n_bad, n_eval = 0, 0
        for rec in per:
            w = [rec[x] for x in (d + timedelta(days=i) for i in range(HORIZON)) if x in rec and x <= t1]
            if len(w) >= 20:
                n_eval += 1
                if min(w) < fatal:
                    n_bad += 1
        if n_eval:
            dangers.append((d, n_bad / n_eval))
        d += timedelta(days=1)
    if not dangers:
        row["verdict"] = "Недостаточно дней прогноза для оценки морозного риска в окне сева"
        return out
    row["rule"] = f"P(хотя бы 1 день с tmin ниже {fatal:g}°C в течение 30 суток после сева) ≤ 10%"
    dmap = dict(dangers)
    if sow_from_d in dmap:
        row["danger_at_sow_from"] = round(dmap[sow_from_d], 2)
    last_bad = None
    for dd, p in dangers:
        if p > 0.10:
            last_bad = dd
    if last_bad is None:
        safe = dangers[0][0]
    else:
        cand = last_bad + timedelta(days=1)
        safe = cand if cand <= end else None
    if safe is None:
        row["verdict"] = (
            f"По прогнозу риск опасного мороза >10% на всё окно сева ({row['sow_from']}–{row['sow_to']}) — "
            "отложить сев или выбрать ранний морозостойкий гибрид"
        )
    else:
        row["safe_date"] = safe.strftime("%d.%m")
        if safe <= sow_to_d:
            row["recommended"] = max(safe, sow_from_d).strftime("%d.%m")
            row["verdict"] = (
                f"Сев безопасен с ~{safe.strftime('%d.%m')} — в пределах окна сорта ({row['sow_from']}–{row['sow_to']})"
            )
        else:
            row["recommended"] = safe.strftime("%d.%m")
            row["verdict"] = (
                f"Окно сорта ({row['sow_from']}–{row['sow_to']}) по прогнозу морозоопасно — "
                f"безопасная дата ~{safe.strftime('%d.%m')}, сверьте со сроком созревания"
            )
    return out
