import hashlib
import json
from html.parser import HTMLParser
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import insert, select

from agrocast.identity.schema import jobs
from agrocast.serve.browser_policy import ASSETS, CONTENT_SECURITY_POLICY

STATIC = Path(__file__).resolve().parents[1] / "static"


class Markup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.bad = []
        self.scripts = []
        self.in_script = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "style" or any(key.startswith("on") or key == "style" for key in attributes):
            self.bad.append((tag, attrs))
        if tag == "script":
            self.in_script = True
            self.scripts.append(attributes)
            if not attributes.get("src"):
                self.bad.append((tag, attrs))
        for key in ("src", "href"):
            value = attributes.get(key, "")
            if value.startswith(("http:", "https:", "//", "javascript:")):
                self.bad.append((tag, attrs))

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_script = False

    def handle_data(self, data):
        if self.in_script and data.strip():
            self.bad.append(data)


@pytest.mark.parametrize("name", ["index.html", "report.html", "value.html", "pilot.html", "login.html"])
def test_all_templates_have_only_local_external_scripts_and_styles(name):
    parser = Markup()
    parser.feed((STATIC / name).read_text())
    assert not parser.bad
    assert parser.scripts
    assert all(script["src"].startswith("/") for script in parser.scripts)


@pytest.mark.parametrize("path,status", [("/login", 200), ("/login.js", 200), ("/pilot.css", 200), ("/health/live", 200), ("/api/capabilities", 200), ("/api/fields", 401), ("/assets/dom.js", 200)])
def test_csp_is_enforced_on_public_and_denied_responses(anonymous, path, status):
    response = anonymous.get(path)
    assert response.status_code == status
    assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
    assert "content-security-policy-report-only" not in response.headers
    assert "unsafe-inline" not in CONTENT_SECURITY_POLICY and "unsafe-eval" not in CONTENT_SECURITY_POLICY
    assert "script-src-attr 'none'" in CONTENT_SECURITY_POLICY
    assert "style-src-attr 'none'" in CONTENT_SECURITY_POLICY
    assert "base-uri 'none'" in CONTENT_SECURITY_POLICY
    assert "object-src 'none'" in CONTENT_SECURITY_POLICY
    assert "https://arena.ai" in CONTENT_SECURITY_POLICY
    assert "https://*.e2b.app" not in CONTENT_SECURITY_POLICY
    assert response.headers["permissions-policy"] == "camera=(), microphone=(), geolocation=()"


@pytest.mark.parametrize("path", ["/", "/workspace", "/index.html", "/value.html"])
def test_authorized_pages_have_csp_and_no_inline_code(clients, path):
    response = clients().get(path)
    assert response.status_code == 200
    assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
    parser = Markup()
    parser.feed(response.text)
    assert not parser.bad
    assert "{{PILOT_WARNING}}" not in response.text


def test_redirect_and_unhandled_error_keep_csp(anonymous, identity, monkeypatch):
    response = anonymous.get("/workspace", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY

    def crash(token):
        raise RuntimeError("private internal detail")

    monkeypatch.setattr(identity, "authenticate", crash)
    from fastapi.testclient import TestClient
    with TestClient(anonymous.app, raise_server_exceptions=False) as client:
        response = client.get("/api/fields")
    assert response.status_code == 500
    assert "private internal detail" not in response.text
    assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY


def test_only_manifest_assets_are_public(anonymous, clients):
    for path in ASSETS:
        response = anonymous.get(path)
        assert response.status_code == 200
        assert response.headers["x-content-type-options"] == "nosniff"
    for path in ("/assets/index.html", "/assets/../report.html", "/assets/%2e%2e/config.json", "/assets/vendor/leaflet/manifest.json"):
        assert anonymous.get(path, follow_redirects=False).status_code in (401, 303)
        assert clients().get(path, follow_redirects=False).status_code in (403, 422)


def test_leaflet_is_pinned_integrity_checked_and_license_is_shipped():
    manifest = json.loads((STATIC / "vendor/leaflet/manifest.json").read_text())
    assert manifest["version"] == "1.9.4"
    assert manifest["integrity"].startswith("sha512-")
    page = (STATIC / "index.html").read_text()
    for name, values in manifest["files"].items():
        assert hashlib.sha256((STATIC / "vendor/leaflet" / name).read_bytes()).hexdigest() == values["sha256"]
    for name in ("leaflet.js", "leaflet.css"):
        assert manifest["files"][name]["sri"] in page
    assert "Copyright" in (STATIC / "vendor/leaflet/LICENSE.txt").read_text()
    assert "tile.openstreetmap" not in (STATIC / "index.js").read_text()


def test_report_html_requires_valid_owner_before_serving(clients, identity_engine, clock):
    owner = clients("operator_a")
    user = owner.get("/api/auth/me").json()["user"]
    job_id = str(uuid4())
    with identity_engine.begin() as connection:
        connection.execute(insert(jobs).values(id=job_id, owner_id=user["id"], organization_id=user["organization_id"], data={"report": {"title": "private"}}, status="succeeded", created_at=clock[0], updated_at=clock[0]))
    response = owner.get("/report.html", params={"job": job_id})
    assert response.status_code == 200
    assert "private" not in response.text
    assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
    for username in ("reader_a", "admin_a", "operator_b", "admin_b"):
        assert clients(username).get("/report.html", params={"job": job_id}).status_code == 404
    assert owner.get("/report.html", params={"job": "legacy"}).status_code == 422
    with identity_engine.connect() as connection:
        assert len(connection.execute(select(jobs)).all()) == 1
