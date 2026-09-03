import json
from datetime import datetime, timezone
from pathlib import Path

import requests

BUSHEL_KG = 25.4012317
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=1d&interval=1d"
STALE_DAYS = 3
CACHE_TTL_H = 12


def _yahoo_quote(symbol, timeout=15):
    r = requests.get(YAHOO.format(symbol=symbol), timeout=timeout, headers={"User-Agent": "AgroCast/1.0"})
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    meta = res["meta"]
    return float(meta["regularMarketPrice"]), int(meta["regularMarketTime"]), meta.get("shortName", symbol)


def fetch_corn_price(timeout=15):
    cents_bushel, ts_corn, contract = _yahoo_quote("ZC=F", timeout)
    usd_rub, ts_rub, _ = _yahoo_quote("USDRUB=X", timeout)
    usd_per_t = (cents_bushel / 100.0) / BUSHEL_KG * 1000.0
    rub_per_t = usd_per_t * usd_rub
    as_of = datetime.fromtimestamp(min(ts_corn, ts_rub), tz=timezone.utc).strftime("%Y-%m-%d")
    return {
        "contract": f"CBOT {contract}",
        "usd_cents_bushel": round(cents_bushel, 2),
        "usd_rub": round(usd_rub, 3),
        "usd_per_t": round(usd_per_t, 2),
        "rub_per_t": round(rub_per_t, 0),
        "as_of": as_of,
        "source": "биржевой фьючерс на кукурузу CBOT (Chicago Board of Trade), USD/т × спотовый курс USD/RUB; обновление ежедневное",
        "fetched_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
    }


def _read(p):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return None


def _age_days(as_of):
    try:
        d = datetime.strptime(as_of, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return (datetime.now(tz=timezone.utc) - d).days
    except Exception:
        return None


def corn_price(data_root, world_dir="world", timeout=15, max_age_days=STALE_DAYS):
    cache = Path(data_root) / "market" / "corn_price.json"
    cached = _read(cache)
    cache_fresh = bool(cached and cached.get("fetched_at") and _age_days(cached.get("as_of", "1900-01-01")) == 0)
    price = None
    if cache_fresh and cached:
        price = cached
    else:
        try:
            price = fetch_corn_price(timeout=timeout)
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(price, ensure_ascii=False))
        except Exception:
            price = cached
    if price is None:
        price = _read(Path(world_dir) / "artifacts" / "market_seed.json")
    if not price:
        raise RuntimeError("нет данных о цене кукурузы: биржа недоступна, кэш пуст, seed отсутствует")
    age = _age_days(price.get("as_of", "1900-01-01"))
    price = dict(price)
    price["stale_days"] = age
    price["stale"] = age is None or age > max_age_days
    if age is not None and age > 1:
        price["source"] += f"; цена на {price['as_of']} (дней с обновления: {age})"
    return price
