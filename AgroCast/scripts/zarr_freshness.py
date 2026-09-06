import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agrocast.core.settings import RuntimeSettings

NAMES = ["daily_region", "sst", "fields_monthly", "strat_snow", "oisst_boxes", "regimes"]
MAX_AGE_DAYS = {
    "daily_region": 14,
    "sst": 120,
    "fields_monthly": 60,
    "strat_snow": 60,
    "oisst_boxes": 90,
    "regimes": 365,
}
REGION_ARTIFACT_MAX_DAYS = 400
REGION_FIELD_MAX_DAYS = 7


def region_freshness(world, data_root):
    from agrocast.region.regions import REGIONS, grid_artifact_path, skill_artifact_path
    from agrocast.serve import region as region_mod

    alerts = []
    now = pd.Timestamp.now()
    for rid in REGIONS:
        for kind, path in (("grid", grid_artifact_path(world, rid)),
                           ("skill", skill_artifact_path(world, rid))):
            tag = path.name
            if not path.exists():
                print(f"region/{tag}: отсутствует -> АЛЕРТ")
                alerts.append(f"{tag} отсутствует")
                continue
            age = max(0, int((now - pd.Timestamp(path.stat().st_mtime, unit="s", tz=None)).total_seconds() // 86400))
            status = "OK" if age <= REGION_ARTIFACT_MAX_DAYS else "АЛЕРТ"
            print(f"region/{tag}: age={age}d, limit={REGION_ARTIFACT_MAX_DAYS}d -> {status}")
            if age > REGION_ARTIFACT_MAX_DAYS:
                alerts.append(f"{tag} ({age}d > {REGION_ARTIFACT_MAX_DAYS}d)")
        age = region_mod.legacy_field_age_s(data_root, rid)
        if age is None:
            print(f"region/{rid} поле: отсутствует -> предупреждение (data/ не в git; рассчитайте поле)")
            continue
        age_d = int(age // 86400)
        status = "OK" if age <= REGION_FIELD_MAX_DAYS * 86400 else "АЛЕРТ"
        print(f"region/{rid} поле: age={age_d}d, limit={REGION_FIELD_MAX_DAYS}d -> {status}")
        if age > REGION_FIELD_MAX_DAYS * 86400:
            alerts.append(f"поле {rid} ({age_d}d > {REGION_FIELD_MAX_DAYS}d)")
    return alerts


def main():
    settings = RuntimeSettings.from_environment()
    cfg = settings.compute_config()
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
    try:
        bad += region_freshness(settings.world_dir, settings.state_dir)
    except Exception as exc:
        print(f"region: ошибка проверки: {exc}")
    if bad:
        print("АЛЕРТ: данные устарели или отсутствуют: " + "; ".join(bad))
        print("Создайте новый проверяемый bundle вне рабочего world; см. docs/production/STATE.md.")
        sys.exit(1)
    print("СВЕЖЕСТЬ: все zarr-сторы и региональные артефакты в пределах норматива")


if __name__ == "__main__":
    main()
