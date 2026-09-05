import json
import re
from uuid import uuid4

import pytest
from sqlalchemy import insert, select

from agrocast.identity.schema import crops, fields, jobs, users
from agrocast.serve.browser_policy import CONTENT_SECURITY_POLICY

pytestmark = pytest.mark.browser


def expect(locator):
    from playwright.sync_api import expect as assertion
    return assertion(locator)
FIELD = 'Кубань </option><img data-xss src=x onerror=window.__xss=1>'
BREEDER = 'Селекционер "\'><svg data-xss onload=window.__xss=2>'
NOTE = 'javascript:alert(1)\n<script data-xss>window.__xss=3</script> Москва & поле'


def login(page, app, password, username="admin_a"):
    page.goto(app.origin + "/login")
    page.get_by_label("Имя пользователя").fill(username)
    page.get_by_label("Пароль", exact=True).fill(password)
    page.get_by_role("button", name="Войти", exact=True).click()
    page.wait_for_url(app.origin + "/")
    expect(page.locator("#identity-status")).to_contain_text("роль")


def workspace(page, app):
    page.goto(app.origin + "/workspace")
    expect(page.locator("#load-status")).to_have_text(re.compile("^Данные загружены"))


def assert_safe(page, origin, check_csp=True):
    assert page.evaluate("window.__xss") == 0
    assert page.locator("[data-xss]").count() == 0
    assert not page.alerts
    assert not page.errors
    assert all(url.startswith(origin + "/") for url in page.requests_seen)
    if check_csp:
        assert page.evaluate("window.__violations") == []


def save_job(app, username, data, status="succeeded"):
    job_id = str(uuid4())
    with app.identity.engine.begin() as connection:
        owner = connection.execute(select(users).where(users.c.username == username)).mappings().one()
        connection.execute(insert(jobs).values(
            id=job_id, owner_id=owner["id"], organization_id=owner["organization_id"], data=data,
            status=status, created_at=app.identity._now(), updated_at=app.identity._now(),
        ))
    return job_id


@pytest.mark.parametrize("bypass_csp", [False, True], ids=["enforced-csp", "dom-without-csp-mask"])
def test_stored_names_round_trip_and_saved_report_reopens_as_text(pages, live_app, account_password, bypass_csp):
    page = pages(bypass_csp)
    login(page, live_app, account_password)
    workspace(page, live_app)
    page.get_by_label("Название поля", exact=True).fill(FIELD)
    page.get_by_label("Точка Краснодарского края").select_option("P01")
    page.get_by_label("Площадь, га", exact=True).fill("120")
    page.get_by_role("button", name="Сохранить поле", exact=True).click()
    expect(page.locator("#ferr")).to_have_text("Поле сохранено.")
    page.get_by_label("Название сорта", exact=True).fill(FIELD)
    page.get_by_label("Селекционер", exact=True).fill(BREEDER)
    page.get_by_label("Заметки", exact=True).fill(NOTE)
    page.get_by_role("button", name="Сохранить сорт", exact=True).click()
    expect(page.locator("#cerr")).to_have_text("Сорт сохранён.")
    page.reload()
    expect(page.locator("#load-status")).to_have_text(re.compile("^Данные загружены"))
    assert page.locator("#fields td").first.inner_text() == FIELD
    assert page.locator("#cropsbox strong").inner_text() == FIELD
    assert page.locator("#cropsbox p.literal").inner_text() == BREEDER
    assert NOTE in page.locator("#cropsbox").inner_text()
    assert page.locator(".leaflet-interactive").count() == 28
    page.locator(".leaflet-interactive").first.click()
    assert FIELD in page.locator(".leaflet-popup-content").inner_text()
    assert_safe(page, live_app.origin, not bypass_csp)
    with live_app.identity.engine.connect() as connection:
        field = connection.execute(select(fields)).mappings().one()
        crop = connection.execute(select(crops)).mappings().one()
    assert field["data"]["name"] == FIELD
    assert crop["data"]["breeder"] == BREEDER
    report = {
        "title": FIELD, "kind": "forecast", "mode": "seasonal", "start": "2026-10", "lat": 46.25, "lon": 38.25,
        "fields_snapshot": [field["data"]], "crop_snapshot": crop["data"],
        "seasons": [{"months": ["2026-10", "2026-11", "2026-12"], "lead": 1, "t2m": {
            "tercile_probs": {"below": 0.2, "normal": 0.3, "above": 0.5},
            "quantiles_c": {"p10": 0, "p50": 5, "p90": 10}, "normal_c": 4,
            "confidence": {"level": "low", "no_skill": True},
        }}],
        "agro": {"insight": {"frost": {"crop": crop["data"]}}, "what_to_do": [{"action": NOTE, "reason": BREEDER}]},
    }
    job_id = save_job(live_app, "admin_a", {"report": report})
    page.locator("#fields").get_by_role("button", name="Изменить", exact=True).click()
    assert page.get_by_label("Название поля", exact=True).input_value() == FIELD
    page.get_by_label("Название поля", exact=True).fill("Текущее поле после отчёта")
    page.get_by_role("button", name="Сохранить поле", exact=True).click()
    expect(page.locator("#fields")).to_contain_text("Текущее поле после отчёта")
    page.locator("#cropsbox").get_by_role("button", name="Изменить", exact=True).click()
    assert page.get_by_label("Селекционер", exact=True).input_value() == BREEDER
    page.get_by_label("Название сорта", exact=True).fill("Текущий сорт после отчёта")
    page.get_by_role("button", name="Сохранить сорт", exact=True).click()
    expect(page.locator("#cropsbox")).to_contain_text("Текущий сорт после отчёта")
    with live_app.identity.engine.connect() as connection:
        assert connection.execute(select(fields.c.id)).scalar_one() == field["id"]
        assert connection.execute(select(crops.c.id)).scalar_one() == crop["id"]
    page.evaluate("localStorage.setItem('agrocast_fields', JSON.stringify([{name: 'Чужое из старого браузера', area: 1000}]))")
    for _ in range(2):
        response = page.goto(live_app.origin + "/report.html?job=" + job_id)
        assert response.status == 200
        assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
        expect(page.locator("#report-status")).to_have_text(re.compile("^Сохранённая запись загружена"))
        assert page.locator("#report-title").inner_text() == FIELD
        body = page.locator("body").inner_text()
        assert FIELD in body and BREEDER in body and NOTE in body
        assert "Чужое из старого браузера" not in body
        assert page.locator(".tbar").count() == 1
        assert_safe(page, live_app.origin, not bypass_csp)
    page.emulate_media(media="print")
    assert page.locator("#pilot-warning").is_visible()
    assert "не агрорекомендация" in page.locator("#pilot-warning").inner_text()
    page.emulate_media(media="screen")
    workspace(page, live_app)
    page.locator("#fields").get_by_role("button", name="Удалить", exact=True).click()
    expect(page.locator("#fields")).to_contain_text("Своих полей пока нет")
    page.locator("#cropsbox").get_by_role("button", name="Удалить", exact=True).click()
    expect(page.locator("#cropsbox")).to_contain_text("Справочник организации пуст")
    assert_safe(page, live_app.origin, not bypass_csp)


@pytest.mark.parametrize("bypass_csp", [False, True], ids=["enforced-csp", "dom-without-csp-mask"])
def test_value_notes_and_metadata_are_literal(pages, live_app, account_password, bypass_csp):
    path = live_app.world / "artifacts/value_report.json"
    report = json.loads(path.read_text())
    report["notes"] = [FIELD, BREEDER, NOTE]
    report["years_span"] = FIELD
    path.write_text(json.dumps(report))
    page = pages(bypass_csp)
    login(page, live_app, account_password)
    page.goto(live_app.origin + "/value.html")
    expect(page.locator("#body")).to_be_visible()
    assert page.locator("#notes p").all_text_contents() == [FIELD, BREEDER, NOTE]
    assert FIELD in page.locator("#meta").inner_text()
    assert page.locator("#segtable tbody tr").count() == 4
    assert_safe(page, live_app.origin, not bypass_csp)


@pytest.mark.parametrize("bypass_csp", [False, True], ids=["enforced-csp", "dom-without-csp-mask"])
def test_failed_job_error_and_api_error_are_text(pages, live_app, account_password, bypass_csp):
    job_id = save_job(live_app, "admin_a", {"error": FIELD, "log": [BREEDER]}, status="failed")
    page = pages(bypass_csp)
    login(page, live_app, account_password)
    page.goto(live_app.origin + "/report.html?job=" + job_id)
    expect(page.locator("#report-error")).to_be_visible()
    assert page.locator("#report-error").inner_text() == FIELD
    assert_safe(page, live_app.origin, not bypass_csp)
    workspace(page, live_app)

    def reject(route):
        if route.request.method == "POST":
            route.fulfill(status=400, content_type="application/json", body=json.dumps({"error": NOTE}))
        else:
            route.continue_()

    page.route("**/api/crops", reject)
    page.get_by_label("Название сорта", exact=True).fill("Пример")
    page.get_by_role("button", name="Сохранить сорт", exact=True).click()
    expect(page.locator("#cerr")).to_contain_text("javascript:")
    assert page.locator("#cerr").inner_text() == NOTE
    assert_safe(page, live_app.origin, not bypass_csp)


def test_csp_really_blocks_inline_handlers_scripts_and_outbound_fetch(pages, live_app):
    page = pages()
    response = page.goto(live_app.origin + "/login")
    assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
    assert page.evaluate("window.__violations") == []
    page.evaluate("""async () => {
        const script = document.createElement('script');
        script.textContent = 'window.__xss = 4';
        document.body.append(script);
        const button = document.createElement('button');
        button.setAttribute('onclick', 'window.__xss = 5');
        document.body.append(button);
        button.click();
        const styled = document.createElement('div');
        styled.setAttribute('style', 'color: red');
        document.body.append(styled);
        try { await fetch('https://outside.invalid/csp-probe', {signal: AbortSignal.timeout(2000)}); } catch {}
    }""")
    expect(page.locator("html")).to_have_attribute("data-csp-connect", "blocked")
    assert page.evaluate("window.__xss") == 0
    directives = {event["directive"] for event in page.evaluate("window.__violations")}
    assert {"script-src-elem", "script-src-attr", "style-src-attr", "connect-src"} <= directives


def test_reader_cannot_mutate_and_no_role_can_start_computation(pages, live_app, account_password):
    page = pages()
    login(page, live_app, account_password, "reader_a")
    workspace(page, live_app)
    assert page.locator("#fname").is_disabled()
    assert page.locator("#c_name").is_disabled()
    assert page.locator("#go").is_disabled()
    result = page.evaluate("""async () => {
        const me = await (await fetch('/api/auth/me')).json();
        const headers = {'Content-Type': 'application/json', 'X-CSRF-Token': me.csrf_token, 'X-Forwarded-User': 'admin_a'};
        const denied = await fetch('/api/fields', {method: 'POST', headers, body: JSON.stringify({name: 'Blocked', point_id: 'P01', area_ha: 1})});
        const compute = await fetch('/api/prepare', {method: 'POST', headers, body: '{}'});
        return {denied: denied.status, compute: compute.status, cookies: document.cookie};
    }""")
    assert result["denied"] == result["compute"] == 403
    assert "__Host-agrocast_session" not in result["cookies"]
    assert_safe(page, live_app.origin)


def test_report_html_does_not_disclose_another_owners_record(pages, live_app, account_password):
    job_id = save_job(live_app, "admin_a", {"report": {"title": "Private owner marker"}})
    page = pages()
    login(page, live_app, account_password, "operator_a")
    response = page.goto(live_app.origin + "/report.html?job=" + job_id)
    assert response.status == 404
    assert "Private owner marker" not in page.locator("body").inner_text()
    assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY


def test_real_cookie_logout_and_history_cannot_reopen_report(pages, live_app, account_password):
    job_id = save_job(live_app, "admin_a", {"report": {"title": "Private history marker"}})
    page = pages()
    login(page, live_app, account_password)
    cookies = page.context.cookies()
    session_cookie = next(cookie for cookie in cookies if cookie["name"] == "__Host-agrocast_session")
    assert session_cookie["secure"] and session_cookie["httpOnly"] and session_cookie["sameSite"] == "Lax"
    page.goto(live_app.origin + "/report.html?job=" + job_id)
    expect(page.locator("#report-status")).to_have_text(re.compile("^Сохранённая запись загружена"))
    workspace(page, live_app)
    page.get_by_role("button", name="Выйти", exact=True).click()
    page.wait_for_url(live_app.origin + "/login")
    assert not any(cookie["name"] == "__Host-agrocast_session" for cookie in page.context.cookies())
    page.go_back()
    page.wait_for_url(live_app.origin + "/login")
    assert "Private history marker" not in page.locator("body").inner_text()
