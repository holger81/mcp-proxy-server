"""PR 4.2: server/catalog API split from plain bearer tokens (decision D3).

On main, any valid client bearer token passed `require_api_access`, so a
leaked read-only token could add/remove upstream servers via
`/api/servers`. Catalog and servers now sit behind `require_admin_api`:
admin session, or a client explicitly granted `can_admin` (default False).
Plain tokens keep working on the MCP endpoint unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from mcp_proxy.client_store import ClientTokenStore
from mcp_proxy.security import (
    SESSION_ADMIN_KEY,
    require_admin_api,
    require_api_access,
)
from mcp_proxy.settings import Settings


def make_app(tmp_path: Path, *, auth_enabled: bool) -> FastAPI:
    app = FastAPI()
    app.state.settings = Settings(
        admin_password="pw" if auth_enabled else "",
        session_secret="s3cret-s3cret-1234" if auth_enabled else "",
    )
    app.state.client_store = ClientTokenStore(tmp_path)
    app.add_middleware(SessionMiddleware, secret_key="test-secret")

    @app.post("/test/login")
    async def _login(request: Request) -> dict:
        request.session[SESSION_ADMIN_KEY] = True
        return {"ok": True}

    admin_api = APIRouter(dependencies=[Depends(require_admin_api)])

    @admin_api.get("/api/servers-thing")
    async def _managed() -> dict:
        return {"ok": True}

    secured = APIRouter(dependencies=[Depends(require_api_access)])

    @secured.get("/api/open-thing")
    async def _open() -> dict:
        return {"ok": True}

    app.include_router(admin_api)
    app.include_router(secured)
    return app


@pytest.fixture
def store(tmp_path: Path):
    return ClientTokenStore(tmp_path)


def test_bearer_without_capability_gets_clear_403(tmp_path: Path) -> None:
    rec, plain = ClientTokenStore(tmp_path).create("script")
    assert rec.can_admin is False  # D3 default
    client = TestClient(make_app(tmp_path, auth_enabled=True))

    r = client.get("/api/servers-thing", headers={"Authorization": f"Bearer {plain}"})
    assert r.status_code == 403
    assert "can_admin" in r.json()["detail"]

    # Same token still works on the plain require_api_access surface (MCP flow).
    r2 = client.get("/api/open-thing", headers={"Authorization": f"Bearer {plain}"})
    assert r2.status_code == 200


def test_bearer_with_capability_allowed(tmp_path: Path) -> None:
    store = ClientTokenStore(tmp_path)
    rec, plain = store.create("ops")
    updated = store.update(rec.id, can_admin=True)
    assert updated is not None and updated.can_admin is True

    client = TestClient(make_app(tmp_path, auth_enabled=True))
    r = client.get("/api/servers-thing", headers={"Authorization": f"Bearer {plain}"})
    assert r.status_code == 200

    # And back off again → 403 immediately (no caching issue).
    store.update(rec.id, can_admin=False)
    r2 = client.get("/api/servers-thing", headers={"Authorization": f"Bearer {plain}"})
    assert r2.status_code == 403


def test_admin_session_allowed(tmp_path: Path) -> None:
    client = TestClient(make_app(tmp_path, auth_enabled=True))
    assert client.get("/api/servers-thing").status_code == 401
    client.post("/test/login")
    assert client.get("/api/servers-thing").status_code == 200


def test_unknown_token_still_401(tmp_path: Path) -> None:
    client = TestClient(make_app(tmp_path, auth_enabled=True))
    r = client.get(
        "/api/servers-thing", headers={"Authorization": "Bearer mcp_nope"}
    )
    assert r.status_code == 401


def test_auth_disabled_passes_through(tmp_path: Path) -> None:
    client = TestClient(make_app(tmp_path, auth_enabled=False))
    assert client.get("/api/servers-thing").status_code == 200


def test_legacy_store_file_backfills_can_admin(tmp_path: Path) -> None:
    # A client record written before the field existed must load as False.
    cfg = tmp_path / "config"
    cfg.mkdir(parents=True)
    (cfg / "api_clients.json").write_text(
        json.dumps(
            {
                "clients": [
                    {
                        "id": "cold",
                        "label": "legacy",
                        "created_at": "2025-01-01T00:00:00Z",
                        "token_sha256_hex": "ab" * 32,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    rec = ClientTokenStore(tmp_path).get("cold")
    assert rec is not None
    assert rec.can_admin is False
