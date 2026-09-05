import hashlib
import json
import os
import shutil
import socket
import subprocess
import threading
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace

import pytest
import uvicorn

from agrocast.identity.service import IdentityService
from agrocast.serve import product

BASE = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def security_browser():
    if os.environ.get("AGROCAST_BROWSER_TESTS") != "1":
        pytest.skip("Enable AGROCAST_BROWSER_TESTS=1 to run real browser scenarios")
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        engine = os.environ.get("AGROCAST_BROWSER_ENGINE", "chromium")
        executable = os.environ.get("AGROCAST_BROWSER_EXECUTABLE")
        options = {"headless": True}
        if executable:
            options["executable_path"] = executable
        if engine == "chromium":
            options["args"] = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
        browser = getattr(playwright, engine).launch(**options)
        print("Browser engine:", engine, browser.version)
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture
def live_app(security_browser, identity, tmp_path, monkeypatch):
    world = tmp_path / "world"
    (world / "artifacts").mkdir(parents=True)
    for name in ("value_report.json", "krai_grid.json", "krai_grid_skill.json"):
        shutil.copyfile(BASE / "world/artifacts" / name, world / "artifacts" / name)
    monkeypatch.setattr(product, "WORLD", str(world))
    cert = tmp_path / "test-cert.pem"
    key = tmp_path / "test-key.pem"
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
        "-keyout", str(key), "-out", str(cert), "-subj", "/CN=localhost",
        "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
    ], check=True, capture_output=True)
    key.chmod(0o600)
    listener = socket.socket()
    listener.bind(("0.0.0.0", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    origin = "https://127.0.0.1:" + str(port)
    service = IdentityService(identity.engine, origin, clock=identity.clock)
    application = product.create_app(service)
    ready = threading.Event()

    class Server(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets)
            ready.set()

    config = uvicorn.Config(application, host="0.0.0.0", port=port, ssl_certfile=str(cert), ssl_keyfile=str(key), access_log=False, log_config=None, timeout_keep_alive=1, timeout_graceful_shutdown=2)
    server = Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        assert ready.wait(15), "HTTPS test server did not start"
        yield SimpleNamespace(origin=origin, identity=service, world=world, application=application)
    finally:
        server.should_exit = True
        thread.join(15)
        listener.close()
        assert not thread.is_alive(), "HTTPS test server did not stop"


@pytest.fixture
def pages(security_browser, live_app, request):
    opened = []
    with ExitStack() as stack:
        def create(bypass_csp=False):
            context = security_browser.new_context(ignore_https_errors=True, bypass_csp=bypass_csp, viewport={"width": 1320, "height": 960})
            stack.callback(context.close)
            context.add_init_script("window.__xss = 0; window.__violations = []; document.addEventListener('securitypolicyviolation', event => { window.__violations.push({directive: event.effectiveDirective, blocked: event.blockedURI}); if (event.effectiveDirective === 'connect-src') document.documentElement.dataset.cspConnect = 'blocked'; });")
            page = context.new_page()
            opened.append(page)
            page.errors = []
            page.requests_seen = []
            page.alerts = []
            page.on("pageerror", lambda error: page.errors.append(str(error)))
            page.on("request", lambda request: page.requests_seen.append(request.url))

            def dialog_open(dialog):
                if dialog.type == "alert":
                    page.alerts.append(dialog.message)
                dialog.accept()

            page.on("dialog", dialog_open)
            return page

        yield create
        if getattr(request.node, "browser_failed", False):
            directory = Path(os.environ.get("AGROCAST_BROWSER_ARTIFACTS", "data/browser"))
            directory.mkdir(parents=True, exist_ok=True)
            slug = hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:16]
            for index, page in enumerate(opened):
                if page.is_closed():
                    continue
                page.screenshot(path=str(directory / f"{slug}-{index}.png"), full_page=True, mask=[page.locator('input[type="password"]')])
                evidence = {"errors": page.errors, "alerts": page.alerts, "csp": page.evaluate("window.__violations || []")}
                (directory / f"{slug}-{index}.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    result = yield
    report = result.get_result()
    if report.when == "call":
        item.browser_failed = report.failed
