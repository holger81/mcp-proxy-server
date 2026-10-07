"""PR 7.2 regression: the login page must work with auth enabled.

7.2 externalized the login form script to ``/admin/login.js``. The auth
middleware only exempted ``login.html``, so the browser's script fetch
(``Accept: */*``) got a 302 to the login page, the HTML failed the script
MIME/CSP checks, and the login form silently stopped working. These tests
exercise the real ``AuthEnforcementMiddleware`` and pin both sides: login
page + its script are public, everything else stays behind the redirect.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from mcp_proxy.security import AuthEnforcementMiddleware
from mcp_proxy.settings import Settings

REPO = Path(__file__).resolve().parents[1]
ADMIN_DIR = REPO / "static" / "admin"


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    served = tmp_path / "admin"
    shutil.copytree(ADMIN_DIR, served)
    settings = Settings(
        admin_password="test-password",
        session_secret="s3cret-s3cret-1234",
    )
    assert settings.auth_enabled
    app = FastAPI()
    # Same stacking as app.create_app: add_middleware prepends, so the
    # session middleware added last wraps (and thus populates for) auth.
    app.add_middleware(AuthEnforcementMiddleware, settings=settings)
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret)
    app.mount("/admin", StaticFiles(directory=str(served), html=True))
    return TestClient(app)


def test_login_page_is_public(client: TestClient) -> None:
    r = client.get("/admin/login.html", headers={"Accept": "text/html"})
    assert r.status_code == 200


def test_login_script_is_public_like_a_browser_fetch(client: TestClient) -> None:
    # Browsers fetch <script src> with Accept: */*; any redirect here hands
    # back HTML and the form breaks (the 7.2 regression).
    r = client.get(
        "/admin/login.js", headers={"Accept": "*/*"}, follow_redirects=False
    )
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]


def test_admin_page_redirects_to_login(client: TestClient) -> None:
    # TestClient follows redirects by default; we want the raw 302.
    r = client.get("/admin/", headers={"Accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/admin/login.html"


def test_dashboard_script_stays_protected(client: TestClient) -> None:
    # app.js is only used after login; it must NOT join the public set.
    r = client.get("/admin/app.js", headers={"Accept": "*/*"}, follow_redirects=False)
    assert r.status_code == 302
