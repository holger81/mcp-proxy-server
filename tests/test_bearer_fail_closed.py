"""PR 4.3: bearer resolution errors fail closed (401), never anonymous.

On main, `McpLiveTrackerMiddleware` swallowed *any* client-store exception
during `resolve_bearer` and bound an anonymous context — so a request whose
token still passed `verify_bearer` (transient first read, different code
path) ran the MCP endpoint **without** per-client tool policy or limits.
The auth middleware had the same shape on `verify_bearer`: store errors
surfaced as 500s instead of clean 401s.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from mcp_proxy.client_store import ApiClientRecord, ClientTokenStore
from mcp_proxy.live_mcp_tracker import current_mcp_api_client_id
from mcp_proxy.mcp_live_tracker_middleware import McpLiveTrackerMiddleware
from mcp_proxy.security import (
    AuthEnforcementMiddleware,
    require_admin_api,
)
from mcp_proxy.settings import Settings

RECORD = ApiClientRecord(
    id="cscript",
    label="script",
    created_at="2026-01-01T00:00:00Z",
    token_sha256_hex="0" * 64,
)


class FlakyStore:
    """Raises on the first N calls per method, then delegates normally."""

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.resolve_calls = 0
        self.verify_calls = 0

    def resolve_bearer(self, token: str) -> ApiClientRecord | None:
        self.resolve_calls += 1
        if self.resolve_calls <= self.failures:
            raise OSError("api_clients.json: resource temporarily unavailable")
        return RECORD if token == "good" else None

    def verify_bearer(self, token: str) -> bool:
        self.verify_calls += 1
        if self.verify_calls <= self.failures:
            raise OSError("api_clients.json: resource temporarily unavailable")
        return token == "good"


class Tracker:
    async def touch(self, **kwargs) -> None:  # pragma: no cover - unused
        pass


async def _echo(scope, receive, send) -> None:
    body = json.dumps({"cid": current_mcp_api_client_id.get()}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": body})


def tracker_client(store) -> TestClient:
    mw = McpLiveTrackerMiddleware(_echo, Tracker(), store)
    return TestClient(mw)


def test_tracker_rejects_bearer_when_store_keeps_raising() -> None:
    client = tracker_client(FlakyStore(failures=99))
    r = client.get("/mcp", headers={"Authorization": "Bearer good"})
    assert r.status_code == 401  # not 500, not an anonymous pass-through
    assert "detail" in r.json()


def test_tracker_recovers_after_transient_error() -> None:
    store = FlakyStore(failures=1)
    client = tracker_client(store)
    r = client.get("/mcp", headers={"Authorization": "Bearer good"})
    assert r.status_code == 200  # retry succeeded
    assert r.json()["cid"] == "cscript"
    assert store.resolve_calls == 2


def test_tracker_passes_anonymous_requests_through() -> None:
    client = tracker_client(FlakyStore(failures=99))
    r = client.get("/mcp")  # no bearer → store not consulted at all
    assert r.status_code == 200
    assert r.json()["cid"] is None


def _authed_app(store, tmp_path: Path) -> FastAPI:
    settings = Settings(admin_password="pw", session_secret="s3cret-s3cret-1234")
    app = FastAPI()
    app.state.settings = settings
    app.state.client_store = store
    # add_middleware prepends: add Auth first so Session ends up outermost.
    app.add_middleware(AuthEnforcementMiddleware, settings=settings)
    app.add_middleware(SessionMiddleware, secret_key="test-secret")

    admin_api = APIRouter(dependencies=[Depends(require_admin_api)])

    @admin_api.get("/api/servers-thing")
    async def _managed() -> dict:
        return {"ok": True}

    app.include_router(admin_api)

    @app.get("/mcp")
    async def _mcp() -> dict:
        return {"ok": True}

    return app


def test_auth_middleware_store_error_is_401_not_500(tmp_path: Path) -> None:
    client = TestClient(_authed_app(FlakyStore(failures=99), tmp_path))
    r = client.get(
        "/mcp",
        headers={"Authorization": "Bearer good", "Accept": "application/json"},
    )
    assert r.status_code == 401


def test_admin_api_dep_store_error_is_401(tmp_path: Path) -> None:
    client = TestClient(_authed_app(FlakyStore(failures=99), tmp_path))
    r = client.get(
        "/api/servers-thing", headers={"Authorization": "Bearer good"}
    )
    assert r.status_code == 401


def test_real_store_still_works(tmp_path: Path) -> None:
    store = ClientTokenStore(tmp_path)
    rec, plain = store.create("ops")
    store.update(rec.id, can_admin=True)
    client = TestClient(_authed_app(store, tmp_path))
    assert (
        client.get("/mcp", headers={"Authorization": f"Bearer {plain}"}).status_code
        == 200
    )
    assert (
        client.get(
            "/api/servers-thing", headers={"Authorization": f"Bearer {plain}"}
        ).status_code
        == 200
    )
