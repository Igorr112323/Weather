import os
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agrocast.serve.pipeline import world_config

WORLD = os.environ.get(
    "AGROCAST_WORLD", str(Path(__file__).resolve().parent.parent / "world")
)
NAMES = ["daily_region", "sst", "fields_monthly", "strat_snow", "oisst_boxes", "regimes"]
MAX_AGE_DAYS = {
    "daily_region": 14,
    "sst": 120,
    "fields_monthly": 60,
    "strat_snow": 60,
    "oisst_boxes": 90,
    "regimes": 365,
}


def main():
    cfg = world_config(WORLD)
    st = cfg.zarr_store()
    now = pd.Timestamp.now()
    bad = []
    for name in NAMES:
        if not st.exists(name):
            print(f"{name}: store отсутствует — пропущено")
            continue
        last = st.last_time(name)
        if last.tzinfo is not None:
            last = last.tz_localize(None)
        age = (now - last).days
        limit = MAX_AGE_DAYS[name]
        status = "OK" if age <= limit else "УСТАРЕЛ"
        print(f"{name}: last={last.date()}, age={age}d, limit={limit}d -> {status}")
        if age > limit:
            bad.append(f"{name} ({age}d > {limit}d)")
    if bad:
        print("АЛЕРТ: zarr-данные устарели: " + "; ".join(bad))
        print("Обновите zarr в AgroCast/world/zarr (см. README_ЗАПУСК.txt) и перезакоммитьте.")
        sys.exit(1)
    print("СВЕЖЕСТЬ: все zarr-сторы в пределах норматива")


if __name__ == "__main__":
    main()
