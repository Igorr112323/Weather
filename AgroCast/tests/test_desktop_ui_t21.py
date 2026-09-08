from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agrocast.core.settings import RuntimeSettings
from agrocast.serve.product import create_app

ORIGIN = "https://127.0.0.1"
POINT = {"lat": 45.03, "lon": 39.07, "start": "2026-03", "horizon": 3, "mode": "seasonal", "season_len": 3}


@pytest.fixture
def desktop_settings(tmp_path):
    return RuntimeSettings(world_dir=Path(__file__).resolve().parents[1] / "world", state_dir=tmp_path / "state", public_origin=ORIGIN, desktop_mode=True)


@pytest.fixture
def desktop_client(desktop_settings):
    application = create_app(settings=desktop_settings)
    with TestClient(application, base_url=ORIGIN) as client:
        yield client


def test_desktop_page_declares_states_and_cancel(desktop_client):
    page = desktop_client.get("/desktop.html")
    assert page.status_code == 200
    assert 'id="cancel"' in page.text
    assert 'role="status"' in page.text
    assert 'aria-live="polite"' in page.text
    assert 'name="viewport"' in page.text


def test_desktop_page_is_single_view_with_leaflet_map(desktop_client):
    page = desktop_client.get("/desktop.html")
    assert page.status_code == 200
    text = page.text
    # одна вкладка: старые вкладки и отдельные секции удалены
    assert "data-tab" not in text
    assert "hindcast-tab" not in text
    assert "reports-tab" not in text
    assert "hindcast-map" not in text
    # Leaflet-карта с SRI-пinned ассетами
    assert "/assets/vendor/leaflet/leaflet.css" in text
    assert "/assets/vendor/leaflet/leaflet.js" in text
    assert "integrity=" in text
    assert 'id="map"' in text
    # интервал 1/3/6 месяцев
    assert 'id="horizon"' in text
    for label in ("1 месяц", "3 месяца", "6 месяцев"):
        assert label in text
    # без упоминания «локальный режим»
    assert "локальный режим" not in text
    assert desktop_client.get("/").status_code == 200
    assert "локальный режим" not in desktop_client.get("/").text


def test_desktop_css_has_focus_mobile_and_print(desktop_client):
    css = desktop_client.get("/assets/desktop.css")
    assert css.status_code == 200
    assert ":focus-visible" in css.text
    assert "@media (max-width:700px)" in css.text
    assert "@media print" in css.text
    assert "size:A4" in css.text


def test_desktop_js_wires_abort_and_readonly_errors(desktop_client):
    js = desktop_client.get("/assets/desktop.js")
    assert js.status_code == 200
    for marker in ["AbortController", "signal: currentAbort.signal", "clearInterval", "describeError", "timedOut"]:
        assert marker in js.text
    assert "[object Object]" not in js.text
    assert "Ресурс не найден (404)" in js.text
    assert "Проверьте форму" in js.text


def test_desktop_js_snaps_map_click_to_grid_and_auto_hindcast(desktop_client):
    js = desktop_client.get("/assets/desktop.js").text
    # клик в любую точку карты → snap к ближайшей точке сетки
    assert "L.map(" in js
    assert "nearestGridPoint" in js
    assert "distanceTo" in js
    # тайлы OSM разрешены и используются
    assert "tile.openstreetmap.org" in js
    # прошлые даты уходят в hindcast автоматически
    assert "/api/local/hindcast" in js
    assert "/api/local/forecast" in js
    # реальные причины ошибок читаются из поля error
    assert "body.error" in js


def test_desktop_csp_allows_osm_tiles(desktop_client):
    from agrocast.serve import browser_policy

    response = desktop_client.get("/desktop.html")
    csp = response.headers["content-security-policy"]
    assert csp == browser_policy.CONTENT_SECURITY_POLICY
    assert "https://tile.openstreetmap.org" in browser_policy.CONTENT_SECURITY_POLICY
    assert "img-src 'self' data: https://tile.openstreetmap.org" in browser_policy.CONTENT_SECURITY_POLICY


def test_unknown_api_path_returns_structured_404(desktop_client):
    response = desktop_client.get("/api/local/definitely-not-here")
    assert response.status_code == 404


def test_invalid_forecast_request_returns_readable_422(desktop_client):
    response = desktop_client.post("/api/local/forecast", json={"lat": "горячий", "lon": 39.07, "start": "2026-03"})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, list) and detail
    assert isinstance(detail[0].get("loc"), list)
    assert "[object Object]" not in str(detail)


def test_forecast_then_reopen_from_cache_end_to_end(desktop_client):
    first = desktop_client.post("/api/local/forecast", json=POINT)
    assert first.status_code == 200
    payload_first = first.json()
    assert payload_first["cached"] is False
    assert payload_first["payload"]["seasons"]
    listing = desktop_client.get("/api/local/inputs").json()
    assert listing["results_cache"]["total"] >= 1
    assert listing["generated_at"] > 0
    second = desktop_client.post("/api/local/forecast", json=POINT)
    assert second.status_code == 200
    payload_second = second.json()
    assert payload_second["cached"] is True
    assert payload_second["payload"]["seasons"] == payload_first["payload"]["seasons"]
