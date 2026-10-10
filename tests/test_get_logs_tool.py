"""PR 41: ``getLogs`` admin MCP tool — ring-buffer logs via /mcp.

Exposes the same in-memory buffer as ``GET /api/logs`` (admin UI Logs tab) to
admin-scoped MCP clients. Being in ``_ADMIN_TOOL_NAMES``, the PR 38 gate
covers it: warn-first by default, denied with ``enforce_mcp_admin_tools`` for
clients lacking ``can_admin``.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from mcp import types as mcp_types

import mcp_proxy.proxy_mcp as pm
from mcp_proxy.client_store import ApiClientRecord
from mcp_proxy.live_mcp_tracker import current_mcp_api_client
from mcp_proxy.log_buffer import RingLogHandler, get_ring_handler
from mcp_proxy.models import UpstreamServer
from mcp_proxy.settings import Settings
from mcp_proxy.tool_call_stats import ToolCallStatsStore


class FakeStore:
    def __init__(self) -> None:
        self._servers = {s.id: s for s in [UpstreamServer(id="s1", type="http", url="http://x/mcp")]}

    def list_servers(self):
        return list(self._servers.values())

    def get(self, sid: str):
        return self._servers.get(sid)


def _build(tmp_path, *, enforce: bool = False):
    settings = Settings(data_dir=tmp_path, enforce_mcp_admin_tools=enforce)
    stats = ToolCallStatsStore(tmp_path, flush_interval_s=3600.0)
    srv = pm.build_proxy_mcp_server(
        FakeStore(), SimpleNamespace(list_records=lambda: []), settings, stats
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


def _as_text(result) -> str:
    return "".join(c.text for c in result.content if c.type == "text")


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


@pytest.fixture()
def seeded_buffer():
    ring = get_ring_handler()
    saved = list(ring.get_lines())
    ring._buf.clear()
    logger = logging.getLogger("mcp_proxy.test_getlogs")
    logger.setLevel(logging.INFO)
    logger.addHandler(ring)
    logger.info("alpha one")
    logger.warning("Beta TWO can_admin scope")
    logger.error("gamma three")
    yield ring
    logger.removeHandler(ring)
    ring._buf.clear()
    for line in saved:
        ring._buf.append(line)


async def test_get_logs_returns_buffer(seeded_buffer, tmp_path):
    srv = _build(tmp_path)
    call = _caller(srv)
    token = current_mcp_api_client.set(_client(can_admin=True))
    try:
        result = await call("getLogs", {})
    finally:
        current_mcp_api_client.reset(token)
    assert not result.isError
    text = _as_text(result)
    assert "alpha one" in text and "gamma three" in text


async def test_contains_filter_is_case_insensitive(seeded_buffer, tmp_path):
    srv = _build(tmp_path)
    call = _caller(srv)
    token = current_mcp_api_client.set(_client(can_admin=True))
    try:
        result = await call("getLogs", {"contains": "CAN_ADMIN"})
    finally:
        current_mcp_api_client.reset(token)
    assert not result.isError
    text = _as_text(result)
    assert "Beta TWO can_admin scope" in text
    assert "alpha one" not in text


async def test_limit_slices_tail(seeded_buffer, tmp_path):
    srv = _build(tmp_path)
    call = _caller(srv)
    token = current_mcp_api_client.set(_client(can_admin=True))
    try:
        result = await call("getLogs", {"limit": 1})
    finally:
        current_mcp_api_client.reset(token)
    text = _as_text(result)
    assert "gamma three" in text and "alpha one" not in text


async def test_empty_buffer_message(seeded_buffer, tmp_path):
    seeded_buffer._buf.clear()
    srv = _build(tmp_path)
    call = _caller(srv)
    token = current_mcp_api_client.set(_client(can_admin=True))
    try:
        result = await call("getLogs", {})
        assert _as_text(result) == "(log buffer empty)"
        result = await call("getLogs", {"contains": "nope"})
        assert _as_text(result) == "(no matching log lines)"
    finally:
        current_mcp_api_client.reset(token)


@pytest.mark.parametrize(
    ("args", "snippet"),
    [
        ({"limit": 0}, "'limit'"),
        ({"limit": -5}, "'limit'"),
        ({"limit": "many"}, "'limit'"),
        ({"limit": True}, "'limit'"),
        ({"contains": 42}, "'contains'"),
    ],
)
async def test_invalid_arguments(seeded_buffer, tmp_path, args, snippet):
    srv = _build(tmp_path)
    call = _caller(srv)
    token = current_mcp_api_client.set(_client(can_admin=True))
    try:
        result = await call("getLogs", args)
    finally:
        current_mcp_api_client.reset(token)
    assert result.isError
    assert snippet in _as_text(result)


async def test_enforce_denies_non_admin(seeded_buffer, tmp_path):
    srv = _build(tmp_path, enforce=True)
    call = _caller(srv)
    token = current_mcp_api_client.set(_client(can_admin=False))
    try:
        result = await call("getLogs", {})
    finally:
        current_mcp_api_client.reset(token)
    assert result.isError
    assert "can_admin" in _as_text(result)


async def test_warn_mode_serves_non_admin(seeded_buffer, tmp_path):
    srv = _build(tmp_path, enforce=False)
    call = _caller(srv)
    token = current_mcp_api_client.set(_client(can_admin=False))
    try:
        result = await call("getLogs", {})
    finally:
        current_mcp_api_client.reset(token)
    assert not result.isError
    assert "alpha one" in _as_text(result)


async def test_discovery_row_present(tmp_path):
    settings = Settings(data_dir=tmp_path)
    rows = pm._admin_tool_rows(settings)
    get_logs = [r for r in rows if r["_proxyUpstreamTool"] == "getLogs"]
    assert len(get_logs) == 1
    row = get_logs[0]
    assert row["domain"] == pm._ADMIN_DOMAIN_ID
    assert set(row["inputSchema"]["properties"]) == {"limit", "contains"}
    # composite wire name resolves back through the hot-path lookup
    assert pm._split_proxy_tool_name(row["toolName"]) == (
        pm._ADMIN_SERVER_ID,
        "getLogs",
    )


def test_attach_ring_logging_covers_news_server_logger():
    """PR 42: the in-process digest refresher logs under ``mcp_news_server``;
    its feed-fetch warnings must reach the ring (and getLogs)."""
    from mcp_proxy.log_buffer import attach_ring_logging

    attach_ring_logging()
    parent = logging.getLogger("mcp_news_server")
    assert any(type(h) is RingLogHandler for h in parent.handlers)

    ring = get_ring_handler()
    before = len(ring.get_lines())
    # child logger (propagates to the attached parent)
    logging.getLogger("mcp_news_server.fetchers").warning(
        "feed fetch failed for https://unit.test/rss"
    )
    lines = ring.get_lines()
    assert len(lines) == before + 1
    assert "feed fetch failed for https://unit.test/rss" in lines[-1]
