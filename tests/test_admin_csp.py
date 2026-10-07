"""PLAN 7.2: strict CSP + hardening headers on every ``/admin/*`` response.

Header behavior is tested against the real static/admin files served through
``SecurityHeadersASGI``; the structural tests then pin down the *preconditions*
that make ``script-src 'self'`` (no ``'unsafe-inline'``) viable, so a future
PR that pastes an inline ``<script>`` or an ``onclick=`` attribute back into
the HTML fails CI instead of silently weakening the CSP.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from mcp_proxy.admin_security_headers import ADMIN_CSP, SecurityHeadersASGI

REPO = Path(__file__).resolve().parents[1]
ADMIN_DIR = REPO / "static" / "admin"


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    served = tmp_path / "admin"
    shutil.copytree(ADMIN_DIR, served)
    app = FastAPI()
    app.mount(
        "/admin",
        SecurityHeadersASGI(StaticFiles(directory=str(served), html=True)),
    )
    return TestClient(app)


@pytest.mark.parametrize("path", ["/admin/", "/admin/login.html", "/admin/app.js"])
def test_hardening_headers(client: TestClient, path: str) -> None:
    r = client.get(path)
    assert r.status_code == 200, path
    assert r.headers["content-security-policy"] == ADMIN_CSP
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"


def test_csp_script_src_has_no_unsafe_inline() -> None:
    directives = dict(d.split(" ", 1) for d in ADMIN_CSP.split("; "))
    assert directives["default-src"] == "'self'"
    assert directives["script-src"] == "'self'"
    assert "frame-ancestors" in directives
    assert "object-src" in directives


def test_admin_html_has_no_inline_scripts(client: TestClient) -> None:
    # `client` ensures the app mounts; the real check is on the source files.
    for name, js in (("index.html", "app.js"), ("login.html", "login.js")):
        src = (ADMIN_DIR / name).read_text(encoding="utf-8")
        assert not re.search(r"<script(?![^>]*\bsrc=)", src), name
        assert f'<script src="{js}"></script>' in src, name
        assert (ADMIN_DIR / js).is_file()


def test_admin_html_has_no_inline_event_handlers() -> None:
    for name in ("index.html", "login.html"):
        src = (ADMIN_DIR / name).read_text(encoding="utf-8")
        assert not re.search(r"\son[a-z]+\s*=", src), name


def test_app_mount_wraps_static_files_with_security_headers() -> None:
    src = (REPO / "src" / "mcp_proxy" / "app.py").read_text(encoding="utf-8")
    assert "SecurityHeadersASGI(" in src
    assert re.search(
        r"SecurityHeadersASGI\(\s*StaticFiles\(", src
    ), "admin mount must wrap StaticFiles"
