import argparse
import datetime as dt
import json
import os
import smtplib
import sys
from email.mime.text import MIMEText
from pathlib import Path

from agrocast.core.timeutils import next_occurrence, month_period
from agrocast.geo.russia import inside_russia, nearest_inside_distance_km

MNS = {1: "январь", 2: "февраль", 3: "март", 4: "апрель", 5: "май", 6: "июнь", 7: "июль", 8: "август", 9: "сентябрь", 10: "октябрь", 11: "ноябрь", 12: "декабрь"}

CROP_NAMES = {
    "winter_wheat": "озимая пшеница",
    "winter_barley": "озимый ячмень",
    "sunflower": "подсолнечник",
    "maize": "кукуруза на зерно",
    "soy": "соя",
    "rapeseed": "озимый рапс",
    "sugar_beet": "сахарная свёкла",
}


def rub(x):
    return f"{round(float(x)):,}".replace(",", " ")


def run_one(world, data_root, lat, lon, start, horizon, mode, season_len):
    from agrocast.serve.pipeline import Job, run_job

    job = Job("digest", {"lat": lat, "lon": lon, "start": str(start), "horizon": horizon, "mode": mode, "season_len": season_len, "kind": "forecast"})
    run_job(job, world, data_root)
    if job.status != "done":
        raise RuntimeError(job.error or "прогноз не посчитался")
    return job.result


def _season_line(item):
    months = item.get("months") or []
    yy = str(item.get("year") or (months[0].split("-")[0] if months else ""))
    mm = [int(m.split("-")[1]) for m in months]
    label = f"{MNS.get(mm[0], '?')}–{MNS.get(mm[-1], '?')} {yy}" if len(mm) > 1 else f"{MNS.get(mm[0], '?')} {yy}"
    t = item.get("t2m") or {}
    p = item.get("tp") or {}
    tq = t.get("quantiles_c") or {}
    pq = p.get("quantiles_mm") or {}
    tnorm = t.get("normal_c")
    pnorm = p.get("normal_mm")
    anom = round(float(tq.get("p50", 0)) - float(tnorm), 1) if tq.get("p50") is not None and tnorm is not None else None
    pr = pq.get("p50")
    pr_pct = round(100 * float(pr) / float(pnorm)) if pr is not None and pnorm else None
    tp = t.get("tercile_probs") or {}
    hot = round(100 * float(tp.get("above", 0)))
    return label, anom, pr, pr_pct, hot


def field_letter(field, payload):
    crop_key = field.get("crop")
    area = float(field.get("area") or 0)
    lines = []
    lines.append(f"Поле «{field.get('name', 'без имени')}» — {CROP_NAMES.get(crop_key, crop_key or 'культура не задана')}, {rub(area)} га, {field.get('lat', '?')}°N {field.get('lon', '?')}°E")
    items = payload.get("seasons") or payload.get("months") or []
    for it in items[:2]:
        label, anom, pr, pr_pct, hot = _season_line(it)
        t_txt = f"{anom:+.1f}°C к норме" if anom is not None else "—"
        p_txt = f"{rub(pr)} мм ({pr_pct}% нормы)" if pr is not None else "—"
        lines.append(f"  {label}: температура {t_txt}, вероятность аномально тёплого сезона {hot}%, осадки {p_txt}")
    agro = payload.get("agro") or {}
    ins = agro.get("insight") or {}
    w = ins.get("water") or {}
    irr = w.get("irrigation_m3_ha") or {}
    if w:
        w_st = (w.get("policy") or {}).get("status")
        surf = w.get("surface_theta")
        lines.append(f"  Вода: осадки за сезон {rub((w.get('precip_mm') or {}).get('p50', 0))} мм, " + (f"дефицит {rub(w.get('deficit_mm', 0))} мм" if w.get("deficit_mm", 0) >= 0 else f"профицит {rub(-w.get('deficit_mm', 0))} мм") + (f", влажность слоя 0–7 см θ={surf} м³/м³ (индикатор; запас корнеобитаемого слоя не верифицирован и не рассчитывается)" if surf is not None else ""))
        if w_st != "unavailable" and irr:
            basis = "навык tp подтверждён" if w_st == "confirmed" else "ориентировочно: навык tp не подтверждён — не предписание"
            lines.append(f"  Полив ({basis}): медиана {rub(irr.get('p50', 0))} м³/га, сухой сценарий (ET0 p90 / осадки p10) {rub(irr.get('p10', 0))} м³/га" + (f" → на всё поле {rub(irr.get('p10', 0) * area)} м³" if area else ""))
    ph = agro.get("phenology") or {}
    crop = next((c for c in ph.get("crops", []) if c.get("key") == crop_key), None)
    if crop:
        lines.append(f"  Культура: {crop.get('verdict')}")
        top = sorted([s for s in crop.get("stages", []) if s.get("prob") is not None], key=lambda s: -s["prob"])[:2]
        for s in top:
            lines.append(f"    {s.get('name')} ({s.get('window')}): риск «{s.get('crit')}» {round(100 * s['prob'])}% — {s.get('action')}")
    econ = agro.get("econ") or {}
    e = next((c for c in econ.get("crops", []) if c.get("key") == crop_key), None)
    if e and area:
        lines.append(f"  Деньги (сценарная оценка, не гарантия дохода): ожидаемый убыток без защиты {rub(e['risk_rub_ha'])} ₽/га → на поле {rub(e['risk_rub_ha'] * area)} ₽; полив ({rub(e['cost_irr_rub'])} ₽/га) — {e['irrigation']}, антистресс ({rub(e['cost_anti_rub'])} ₽/га) — {e['antistress']}")
    for d in (agro.get("decisions") or [])[:2]:
        lines.append(f"  Решение: {d.get('label')} — {d.get('verdict')} (вероятность {round(100 * d.get('prob', 0))}%, порог {round(100 * d.get('ratio', 0))}%)")
    lines.append("")
    return "\n".join(lines)


def build_digest(fields, payloads, region_text=None):
    parts = []
    for f, p in zip(fields, payloads):
        parts.append(field_letter(f, p))
    first = (payloads[0].get("seasons") or [{}])[0] if payloads else {}
    label, _, _, _, _ = _season_line(first)
    subject = f"AgroCast: прогноз по вашим полям ({label})" if label and "?" not in label else "AgroCast: прогноз по вашим полям"
    body = "AgroCast — дайджест по полям\n(вероятности из ансамбля; горизонт — сезон)\n\n" + "\n".join(parts)
    if region_text:
        body += "\n" + region_text + "\n"
    body += "Полный отчёт с картой и фено-календарём: откройте продукт и выберите точку на карте.\n"
    return subject, body


def send_mail(subject, body, to_addrs, host, port, user, password, from_addr, use_tls=True):
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = ", ".join(to_addrs)
    with smtplib.SMTP(host, port, timeout=30) as srv:
        if use_tls:
            srv.starttls()
        if user:
            srv.login(user, password)
        srv.sendmail(from_addr, to_addrs, msg.as_string())


def main(argv=None):
    ap = argparse.ArgumentParser(description="AgroCast почтовый дайджест по полям")
    ap.add_argument("--fields", default="fields.json", help="JSON-файл со списком полей")
    ap.add_argument("--start-month", type=int, default=None, help="месяц старта прогноза 1–12 (по умолчанию следующий)")
    ap.add_argument("--year", type=int, default=None, help="год старта (только для прошлого)")
    ap.add_argument("--horizon", type=int, default=3)
    ap.add_argument("--mode", default="seasonal", choices=["monthly", "seasonal"])
    ap.add_argument("--season-len", type=int, default=3)
    ap.add_argument("--to", default=os.environ.get("AGROCAST_TO", ""), help="адреса через запятую")
    ap.add_argument("--dry-run", action="store_true", help="напечатать письмо и выйти")
    args = ap.parse_args(argv)

    from agrocast.core.settings import RuntimeSettings

    settings = RuntimeSettings.from_environment()
    settings.compute_config()
    world, data_root = settings.world_dir, settings.state_dir

    fpath = Path(args.fields)
    if not fpath.exists():
        print(f"файл полей не найден: {fpath}", file=sys.stderr)
        return 2
    fields = json.loads(fpath.read_text(encoding="utf-8"))
    if not fields:
        print("список полей пуст", file=sys.stderr)
        return 2
    for f in fields:
        if not inside_russia(float(f["lat"]), float(f["lon"])):
            print(f"поле «{f.get('name')}» вне территории России (до границы {rub(nearest_inside_distance_km(float(f['lat']), float(f['lon'])))} км) — пропущено", file=sys.stderr)
    fields = [f for f in fields if inside_russia(float(f["lat"]), float(f["lon"]))]
    if not fields:
        print("нет полей внутри России", file=sys.stderr)
        return 2

    m = args.start_month or (dt.date.today().month % 12) + 1
    if args.year:
        start = month_period(args.year, m)
    else:
        start = next_occurrence(m)

    payloads = []
    for f in fields:
        print(f"считаю: {f.get('name')} ({f['lat']}, {f['lon']})", flush=True)
        payloads.append(run_one(world, data_root, float(f["lat"]), float(f["lon"]), start, args.horizon, args.mode, args.season_len))

    region_txt = ""
    try:
        from agrocast.serve import region as region_mod

        if region_mod.grid_path(world).exists():
            region_txt = region_mod.region_block(world, data_root)
    except Exception:
        region_txt = ""
    subject, body = build_digest(fields, payloads, region_txt)
    if args.dry_run:
        print("\nТема: " + subject + "\n\n" + body)
        return 0

    to_addrs = [a.strip() for a in args.to.split(",") if a.strip()]
    if not to_addrs:
        print("не указаны получатели: --to или переменная AGROCAST_TO", file=sys.stderr)
        return 2
    host = os.environ.get("AGROCAST_SMTP_HOST", "")
    if not host:
        print("не указан SMTP-сервер: переменная AGROCAST_SMTP_HOST", file=sys.stderr)
        return 2
    port = int(os.environ.get("AGROCAST_SMTP_PORT", "587"))
    user = os.environ.get("AGROCAST_SMTP_USER", "")
    password = os.environ.get("AGROCAST_SMTP_PASS", "")
    from_addr = os.environ.get("AGROCAST_SMTP_FROM", user or "agrocast@localhost")
    use_tls = os.environ.get("AGROCAST_SMTP_TLS", "1") != "0"
    send_mail(subject, body, to_addrs, host, port, user, password, from_addr, use_tls)
    print(f"отправлено {len(to_addrs)} получателям: {subject}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
