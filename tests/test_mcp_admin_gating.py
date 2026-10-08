"""PR 38 (external audit): admin tools on ``/mcp`` must respect ``can_admin``.

Before this fix, proxy-admin tools (``listServers``, ``setServerEnabled``,
``register*StdioServer``, ``removeServer``) were callable by ANY MCP client on
``/mcp`` while the HTTP admin API required ``can_admin`` (since 4.2). The gate
lives at the top of ``_call_tool_impl``, so direct tools/call, the ``callTool``
composite (``mcp-tools-admin/…`` recurses to the plain name) and hot shortcuts
are all covered. Warn-first per D4: while ``enforce_mcp_admin_tools`` is false,
non-admin callers are warned once per (client, tool) and still served. An
*unbound* client (auth disabled → single-user mode) stays allowed, mirroring
``require_admin_api``.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp import types as mcp_types
from starlette.middleware.sessions import SessionMiddleware

import mcp_proxy.proxy_mcp as pm
from mcp_proxy.client_store import ApiClientRecord
from mcp_proxy.live_mcp_tracker import current_mcp_api_client
from mcp_proxy.models import UpstreamServer
from mcp_proxy.security import AuthEnforcementMiddleware
from mcp_proxy.settings import Settings
from mcp_proxy.tool_call_stats import ToolCallStatsStore


class FakeStore:
    def __init__(self, servers) -> None:
        self._servers = {s.id: s for s in servers}

    def list_servers(self):
        return list(self._servers.values())

    def get(self, sid: str):
        return self._servers.get(sid)

    def update_fields(self, sid, fn):  # pragma: no cover - gate fires earlier
        raise AssertionError("gate must deny before store mutation")


def _build(tmp_path, *, enforce: bool):
    settings = Settings(data_dir=tmp_path, enforce_mcp_admin_tools=enforce)
    stats = ToolCallStatsStore(tmp_path, flush_interval_s=3600.0)
    servers = [UpstreamServer(id="s1", type="http", url="http://x/mcp")]
    srv = pm.build_proxy_mcp_server(
        FakeStore(servers),
        SimpleNamespace(list_records=lambda: []),
        settings,
        stats,
    )
    return srv


def _caller(srv):
    request_handler = srv.request_handlers[mcp_types.CallToolRequest]

    async def call(name: str, arguments: dict):
        req = mcp_types.CallToolRequest(
            method="tools/call",
            params=mcp_types.CallToolRequestParams(name=name, arguments=arguments),
        )
        return (await request_handler(req)).root

    return call


def _client(can_admin: bool) -> ApiClientRecord:
    return ApiClientRecord(
        id=f"client-{'admin' if can_admin else 'plain'}",
        label="t",
        created_at="2026-01-01T00:00:00Z",
        token_sha256_hex="0" * 64,
        can_admin=can_admin,
    )


@pytest.fixture(autouse=True)
def _clean_gate_state():
    pm._warned_mcp_admin_calls.clear()
    yield
    pm._warned_mcp_admin_calls.clear()


# --- warn mode (default) -----------------------------------------------------


async def test_non_admin_warn_mode_still_serves(tmp_path, caplog):
    srv = _build(tmp_path, enforce=False)
    call = _caller(srv)
    token = current_mcp_api_client.set(_client(can_admin=False))
    caplog.set_level(logging.WARNING)
    try:
        result = await call("listServers", {})
    finally:
        current_mcp_api_client.reset(token)
    assert not result.isError
    warnings = [
        rec for rec in caplog.records
        if rec.levelno >= logging.WARNING and "can_admin" in rec.getMessage()
    ]
    assert len(warnings) == 1
    assert "MCP_PROXY_ENFORCE_MCP_ADMIN_TOOLS=1" in warnings[0].getMessage()


async def test_warn_once_per_client_and_tool(tmp_path, caplog):
    srv = _build(tmp_path, enforce=False)
    call = _caller(srv)
    token = current_mcp_api_client.set(_client(can_admin=False))
    caplog.set_level(logging.WARNING)
    try:
        await call("listServers", {})
        await call("listServers", {})
    finally:
        current_mcp_api_client.reset(token)
    assert (
        sum(
            1
            for rec in caplog.records
            if rec.levelno >= logging.WARNING and "can_admin" in rec.getMessage()
        )
        == 1
    )


async def test_non_admin_tool_never_warns(tmp_path, caplog):
    srv = _build(tmp_path, enforce=False)
    call = _caller(srv)
    token = current_mcp_api_client.set(_client(can_admin=False))
    caplog.set_level(logging.WARNING)
    try:
        await call("htmlToPlainText", {"html": "<b>x</b>"})
    finally:
        current_mcp_api_client.reset(token)
    assert not [
        rec for rec in caplog.records
        if rec.levelno >= logging.WARNING and "can_admin" in rec.getMessage()
    ]


# --- enforce mode --------------------------------------------------------------


async def test_non_admin_denied_in_enforce_mode(tmp_path):
    srv = _build(tmp_path, enforce=True)
    call = _caller(srv)
    token = current_mcp_api_client.set(_client(can_admin=False))
    try:
        result = await call("setServerEnabled", {"serverId": "s1", "enabled": False})
    finally:
        current_mcp_api_client.reset(token)
    # The SDK's call_tool decorator converts McpError into an isError result.
    assert result.isError
    assert "can_admin" in result.content[0].text


async def test_calltool_composite_denied_in_enforce_mode(tmp_path):
    """``callTool`` with the composite admin name recurses to the plain tool
    name, so the single gate covers the composite route too."""
    srv = _build(tmp_path, enforce=True)
    call = _caller(srv)
    token = current_mcp_api_client.set(_client(can_admin=False))
    try:
        result = await call(
            "callTool",
            {
                "toolName": "mcp-tools-admin/listServers",
                "arguments": {},
            },
        )
    finally:
        current_mcp_api_client.reset(token)
    assert result.isError
    assert "can_admin" in result.content[0].text


async def test_admin_client_allowed_in_enforce_mode(tmp_path):
    srv = _build(tmp_path, enforce=True)
    call = _caller(srv)
    token = current_mcp_api_client.set(_client(can_admin=True))
    try:
        result = await call("listServers", {})
    finally:
        current_mcp_api_client.reset(token)
    assert not result.isError
    assert "s1" in result.content[0].text


async def test_unbound_client_allowed_in_enforce_mode(tmp_path):
    """No identity bound → auth is disabled (single-user mode). Mirrors
    ``require_admin_api``, which allows everything when auth is off."""
    srv = _build(tmp_path, enforce=True)
    call = _caller(srv)
    result = await call("listServers", {})
    assert not result.isError
    assert "s1" in result.content[0].text


# --- /redoc removed + security gate --------------------------------------------


def test_redoc_url_disabled_on_app(tmp_path, monkeypatch: pytest.MonkeyPatch):
    # mcp_proxy.app builds a module-level ``app = create_app()`` at import
    # time; the 4.7b bind policy refuses the default (0.0.0.0 + no auth), so
    # give the import a valid authenticated configuration.
    monkeypatch.setenv("MCP_PROXY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MCP_PROXY_ADMIN_USER", "tester")
    monkeypatch.setenv("MCP_PROXY_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv(
        "MCP_PROXY_SESSION_SECRET",
        "c2VjcmV0LXNlY3JldC1zZWNyZXQtc2VjcmV0LXNlY3JldC00MA",
    )
    from mcp_proxy.app import create_app

    app = create_app(Settings(data_dir=tmp_path, host="127.0.0.1"))
    assert app.redoc_url is None
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/redoc" not in paths


def test_security_gate_blocks_redoc(tmp_path):
    settings = Settings(
        data_dir=tmp_path,
        admin_password="test-password",
        session_secret="s3cret-s3cret-1234",
    )
    assert settings.auth_enabled
    app = FastAPI()

    @app.get("/redoc")
    async def _redoc_probe():  # pragma: no cover - must never be reached
        return {"served": True}

    # Same stacking as app.create_app (add_middleware prepends).
    app.add_middleware(AuthEnforcementMiddleware, settings=settings)
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret)
    client = TestClient(app)
    resp = client.get("/redoc", headers={"Accept": "application/json"})
    assert resp.status_code == 401
