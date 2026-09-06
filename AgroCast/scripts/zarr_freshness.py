import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agrocast.core.settings import RuntimeSettings
from agrocast.serve import readiness as readiness_service
from agrocast.serve.region import legacy_field_age_s

REGION_FIELD_MAX_DAYS = 7


def region_freshness(world, data_root):
    import json

    import pandas as pd

    from agrocast.region.regions import REGIONS, grid_artifact_path, skill_artifact_path
    from agrocast.serve import readiness

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
            if kind == "grid":
                try:
                    json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    print(f"region/{tag}: повреждён -> АЛЕРТ")
                    alerts.append(f"{tag} повреждён")
                    continue
                print(f"region/{tag}: присутствует")
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                payload = None
            stamp = payload.get("generated_at") if isinstance(payload, dict) else None
            if not stamp:
                print(f"region/{tag}: даты в манифесте нет — предупреждение (filesystem mtime не является критерием)")
                continue
            try:
                when = pd.Timestamp(str(stamp))
            except (TypeError, ValueError):
                print(f"region/{tag}: неразбираемая дата манифеста -> АЛЕРТ")
                alerts.append(f"{tag} дата манифеста неразбираема")
                continue
            if when.tzinfo is not None:
                when = when.tz_localize(None)
            age = max(0, int((now - when).total_seconds() // 86400))
            status = "OK" if age <= readiness.SKILL_STALE_DAYS else "АЛЕРТ"
            print(f"region/{tag}: age={age}d, limit={readiness.SKILL_STALE_DAYS}d (дата манифеста) -> {status}")
            if age > readiness.SKILL_STALE_DAYS:
                alerts.append(f"{tag} ({age}d > {readiness.SKILL_STALE_DAYS}d)")
        age = legacy_field_age_s(data_root, rid)
        if age is None:
            print(f"region/{rid} поле: отсутствует -> предупреждение (data/ не в git; рассчитайте поле)")
            continue
        age_d = int(age // 86400)
        status = "OK" if age_d <= REGION_FIELD_MAX_DAYS else "АЛЕРТ"
        print(f"region/{rid} поле: age={age_d}d, limit={REGION_FIELD_MAX_DAYS}d (filesystem-время, даты в манифесте нет) -> {status}")
        if age_d > REGION_FIELD_MAX_DAYS:
            alerts.append(f"поле {rid} ({age_d}d > {REGION_FIELD_MAX_DAYS}d)")
    return alerts


def run(settings, engine=None, now=None):
    config = settings.compute_config()
    result = readiness_service.evaluate(settings, config, engine=engine, now=now)
    for name, block in result["sources"].items():
        print(f"{name}: status={block['status']}, last={block['last']}, age={block['age_days']}d, limit={block['limit_days']}d")
    print(f"bundle: {result['bundle']['status']} (manifest_present={result['bundle']['manifest_present']})")
    for region_id, row in result["regions"].items():
        print(
            f"region/{region_id}: grid={row['grid']}, skill={row['skill']}, "
            f"skill_date={row['skill_generated_at']}, skill_age={row['skill_age_days']}d"
        )
    if result["queue"]["status"] != "ok":
        print(f"queue: {result['queue']['status']}")
    alerts = list(result["reasons"])
    for tag in result["degraded"]:
        print(f"ДЕГРАДАЦИЯ (не алерт): {tag}")
    alerts += region_freshness(settings.world_dir, settings.state_dir)
    return alerts


def main():
    settings = RuntimeSettings.from_environment()
    try:
        alerts = run(settings)
    except Exception as error:
        print(f"АЛЕРТ: проверка свежести не завершилась: {error}")
        sys.exit(1)
    if alerts:
        print("АЛЕРТ: обязательные входы отсутствуют, повреждены или устарели: " + "; ".join(alerts))
        print("Создайте новый проверяемый bundle вне рабочего world; см. docs/production/STATE.md.")
        sys.exit(1)
    print("СВЕЖЕСТЬ: bundle, zarr-входы и региональные артефакты в пределах норматива (production storage)")


if __name__ == "__main__":
    main()
