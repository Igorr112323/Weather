PILOT_VERSION = "closed-pilot-v4"
PILOT_WARNING = (
    "Закрытый пилот: новые прогнозы и агрорекомендации отключены. "
    "Независимый прогнозный навык и покрытие интервалов не подтверждены: "
    "в исходном аудите найдены временные утечки и ошибки пространственной проверки. "
    "Эти результаты нельзя использовать как основание для агрорешений."
)
PILOT_REGION = "krai"
PILOT_ROWS = (
    (46.25, (38.25, 38.75, 39.25, 39.75, 40.25)),
    (45.75, (37.75, 38.25, 38.75, 39.25, 39.75, 40.25)),
    (45.25, (37.25, 37.75, 38.25, 38.75, 39.25, 39.75, 40.25)),
    (44.75, (37.75, 38.25, 38.75, 39.25, 39.75, 40.25)),
    (44.25, (38.75, 39.25, 39.75, 40.25)),
)
PILOT_COORDINATES = tuple((lat, lon) for lat, lons in PILOT_ROWS for lon in lons)


def pilot_validation() -> dict:
    return {
        "status": "unverified",
        "production_ready": False,
        "agronomic_use_allowed": False,
        "coverage_guaranteed": False,
        "audit_date": "2026-09-05",
        "blocking_findings": ["R06", "R08", "R09", "R10", "R11", "R13"],
        "warning": PILOT_WARNING,
    }


def pilot_points() -> list[dict]:
    return [
        {"id": f"P{i:02d}", "lat": lat, "lon": lon}
        for i, (lat, lon) in enumerate(PILOT_COORDINATES, 1)
    ]


def pilot_capabilities() -> dict:
    return {
        "policy_version": PILOT_VERSION,
        "stage": "closed_pilot",
        "access": {
            "authentication": "server_session", "roles": ["reader", "operator", "admin"],
            "read_only": False, "ownership_required": True,
        },
        "crops": [{"id": "maize", "name": "Кукуруза на зерно"}],
        "regions": [{"id": PILOT_REGION, "name": "Краснодарский край", "inspection_points": pilot_points()}],
        "historical_results": {
            "enabled": True,
            "purpose": "inspection_only",
            "years": [2005, 2024],
            "modes": ["monthly", "seasonal"],
            "leads": [1],
            "season_len": 3,
        },
        "forecast": {"enabled": False, "modes": [], "horizons": [], "season_lengths": []},
        "operations": {
            "hindcast": False,
            "region_refresh": False,
            "crop_mutation": True,
            "field_management": True,
            "subscription_preferences": True,
            "subscriptions": False,
            "job_access": True,
            "agro_recommendations": False,
            "legacy_api": False,
        },
        "permissions": {
            "reader": ["historical_read", "own_resources_read", "organization_crops_read"],
            "operator": ["reader_permissions", "own_fields_write", "own_subscription_preferences_write", "own_finished_jobs_delete"],
            "admin": ["operator_permissions", "organization_users_manage", "organization_crops_write"],
        },
        "validation": pilot_validation(),
    }


def historical_result(payload: dict) -> dict:
    return {**payload, "policy_version": PILOT_VERSION, "validation": pilot_validation()}
