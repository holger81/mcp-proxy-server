"""PR 5.7: per-client instructions without mutating shared Server state.

On main: the listTools handler assigned ``server.instructions = ...`` computed
from the *calling* client's context — every client shared one Server, so a
client with custom instructions poisoned the initialize response of everyone
who connected afterwards.
"""

from __future__ import annotations

from types import SimpleNamespace

from mcp import types as mcp_types

import mcp_proxy.proxy_mcp as pm
from mcp_proxy.live_mcp_tracker import current_mcp_api_client
from mcp_proxy.live_streamable_http_session_manager import (
    LiveBindingStreamableHTTPSessionManager,
)
from mcp_proxy.proxy_mcp import build_proxy_mcp_server, session_initialization_options
from mcp_proxy.settings import Settings
from mcp_proxy.tool_call_stats import ToolCallStatsStore


class FakeStore:
    def __init__(self) -> None:
        self._srv = SimpleNamespace(
            id="ok", display_name=None, llm_context="NOTE-SRV-ONLY", enabled=True
        )

    def list_servers(self):
        return [self._srv]

    def get(self, sid: str):
        return self._srv if sid == "ok" else None


def _client(note: str):
    return SimpleNamespace(
        instructions=note,
        disabled_tools=[],
        llm_limits=SimpleNamespace(has_any_override=lambda: False),
    )


def _build(tmp_path):
    store = FakeStore()
    settings = Settings(data_dir=tmp_path)
    stats = ToolCallStatsStore(tmp_path, flush_interval_s=3600.0)
    srv = build_proxy_mcp_server(
        store, SimpleNamespace(list_records=lambda: []), settings, stats
    )
    return srv, store, settings


def test_two_clients_never_see_each_other_instructions(tmp_path) -> None:
    srv, store, settings = _build(tmp_path)
    tok = current_mcp_api_client.set(_client("CLIENT-A-ONLY"))
    try:
        a = session_initialization_options(srv, store, settings).instructions
        current_mcp_api_client.set(_client("CLIENT-B-ONLY"))
        b = session_initialization_options(srv, store, settings).instructions
    finally:
        current_mcp_api_client.reset(tok)
    assert "CLIENT-A-ONLY" in a and "CLIENT-B-ONLY" not in a
    assert "CLIENT-B-ONLY" in b and "CLIENT-A-ONLY" not in b
    assert "NOTE-SRV-ONLY" in a and "NOTE-SRV-ONLY" in b  # server notes stay


def test_shared_server_instructions_untouched(tmp_path) -> None:
    srv, store, settings = _build(tmp_path)
    before = srv.instructions
    tok = current_mcp_api_client.set(_client("CLIENT-A-ONLY"))
    try:
        opts = session_initialization_options(srv, store, settings)
    finally:
        current_mcp_api_client.reset(tok)
    assert "CLIENT-A-ONLY" in opts.instructions
    assert srv.instructions == before  # no shared-state mutation


async def test_list_tools_no_longer_mutates_instructions(tmp_path, monkeypatch) -> None:
    srv, _store, _settings = _build(tmp_path)

    async def no_tools(upstream, _settings):
        return []

    monkeypatch.setattr(pm, "_list_upstream_tools", no_tools)
    before = srv.instructions
    # Old flow: client A's listTools poisoned Server.instructions, so the
    # *next* client's initialize (which reads it via create_initialization_options)
    # got A's private text.
    tok = current_mcp_api_client.set(_client("CLIENT-A-ONLY"))
    try:
        handler = srv.request_handlers[mcp_types.ListToolsRequest]
        result = (await handler(mcp_types.ListToolsRequest(method="tools/list"))).root
    finally:
        current_mcp_api_client.reset(tok)
    assert srv.instructions == before
    assert "CLIENT-A-ONLY" not in srv.create_initialization_options().instructions
    names = {t.name for t in result.tools}
    assert "callTool" in names


def test_manager_falls_back_without_factory(tmp_path) -> None:
    mgr = LiveBindingStreamableHTTPSessionManager(
        app=SimpleNamespace(create_initialization_options=lambda: "fallback"),
        stateless=False,
    )
    assert mgr._current_initialization_options() is None

    sentinel = object()
    mgr.initialization_options_factory = lambda: sentinel  # type: ignore[assignment]
    assert mgr._current_initialization_options() is sentinel

    def boom() -> None:
        raise RuntimeError("factory exploded")

    mgr.initialization_options_factory = boom  # type: ignore[assignment]
    assert mgr._current_initialization_options() is None  # defensive fallback
