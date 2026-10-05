"""PR 5.6: discovery reports *why* tools are missing instead of hiding it.

On main: upstreams that failed during discovery vanished silently, and
searchTool cut matches at tool_search_max_matches without saying so — an LLM
read the shortened list as "these tools do not exist".
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from mcp import types as mcp_types

import mcp_proxy.proxy_mcp as pm
from mcp_proxy.proxy_mcp import _collect_all_tool_defs, build_proxy_mcp_server
from mcp_proxy.settings import Settings
from mcp_proxy.tool_call_stats import ToolCallStatsStore


def _tool(name: str) -> mcp_types.Tool:
    return mcp_types.Tool(
        name=name, description="d", inputSchema={"type": "object", "properties": {}}
    )


def _server(sid: str, domain: str = "default") -> SimpleNamespace:
    return SimpleNamespace(id=sid, domain=domain, enabled=True, llm_context=None)


class FakeStore:
    def __init__(self, servers: list[SimpleNamespace]) -> None:
        self._servers = {s.id: s for s in servers}

    def list_servers(self) -> list[SimpleNamespace]:
        return list(self._servers.values())

    def get(self, sid: str):
        return self._servers.get(sid)


def _patch_list(monkeypatch, tool_map: dict[str, list[mcp_types.Tool]]) -> None:
    async def fake(upstream, _settings):
        if upstream.id not in tool_map:
            raise ConnectionError(f"{upstream.id} down")
        return tool_map[upstream.id]

    monkeypatch.setattr(pm, "_list_upstream_tools", fake)


def _build(tmp_path, monkeypatch, servers, tool_map, *, search_max: int = 25):
    settings = Settings(data_dir=tmp_path, tool_search_max_matches=search_max)
    stats = ToolCallStatsStore(tmp_path, flush_interval_s=3600.0)
    domain_store = SimpleNamespace(list_records=lambda: [])
    _patch_list(monkeypatch, tool_map)
    srv = build_proxy_mcp_server(FakeStore(servers), domain_store, settings, stats)

    request_handler = srv.request_handlers[mcp_types.CallToolRequest]

    async def call(name: str, arguments: dict) -> list[mcp_types.ContentBlock]:
        req = mcp_types.CallToolRequest(
            method="tools/call",
            params=mcp_types.CallToolRequestParams(name=name, arguments=arguments),
        )
        server_result = await request_handler(req)
        result = server_result.root  # types.CallToolResult
        assert not result.isError, result.content
        return result.content

    return call


# -- _collect_all_tool_defs ------------------------------------------------------


async def test_collect_reports_degraded_upstreams(tmp_path, monkeypatch) -> None:
    tools = {"ok": [_tool("alpha")]}

    async def fake(upstream, _settings):
        if upstream.id == "slow":
            raise TimeoutError()
        if upstream.id not in tools:
            raise ConnectionError(f"{upstream.id} down")
        return tools[upstream.id]

    monkeypatch.setattr(pm, "_list_upstream_tools", fake)
    store = FakeStore([_server("ok"), _server("flaky"), _server("slow")])
    defs, degraded = await _collect_all_tool_defs(store, Settings(data_dir=tmp_path), None)
    names = {d["toolName"] for d in defs}
    assert "ok__alpha" in names
    by_id = {d["serverId"]: d["error"] for d in degraded}
    assert by_id["slow"] == "timeout"
    assert "down" in by_id["flaky"]


async def test_healthy_collect_has_empty_degraded(tmp_path, monkeypatch) -> None:
    _patch_list(monkeypatch, {"ok": [_tool("alpha")]})
    store = FakeStore([_server("ok")])
    defs, degraded = await _collect_all_tool_defs(store, Settings(data_dir=tmp_path), None)
    assert degraded == [] and defs


# -- searchTool handler ------------------------------------------------------------


async def test_searchtool_healthy_payload_unchanged(tmp_path, monkeypatch) -> None:
    handler = _build(
        tmp_path,
        monkeypatch,
        [_server("ok")],
        {"ok": [_tool("alpha")]},
    )
    out = await handler("searchTool", {"query": "alpha"})
    assert len(out) == 1  # byte-identical contract: single array block
    rows = json.loads(out[0].text)
    assert isinstance(rows, list)
    assert rows[0]["toolName"] == "ok__alpha"


async def test_searchtool_adds_degraded_block(tmp_path, monkeypatch) -> None:
    handler = _build(
        tmp_path,
        monkeypatch,
        [_server("ok"), _server("flaky")],
        {"ok": [_tool("alpha")]},
    )
    out = await handler("searchTool", {"query": "alpha"})
    assert len(out) == 2
    meta = json.loads(out[1].text)["discoveryMeta"]
    assert meta["truncated"] is False
    (deg,) = meta["degradedServers"]
    assert deg["serverId"] == "flaky" and "flaky down" in deg["error"]


async def test_searchtool_reports_truncation(tmp_path, monkeypatch) -> None:
    handler = _build(
        tmp_path,
        monkeypatch,
        [_server("ok")],
        {"ok": [_tool(f"alpha{i}") for i in range(5)]},
        search_max=2,
    )
    out = await handler("searchTool", {"query": "alpha"})
    rows = json.loads(out[0].text)
    assert len(rows) == 2
    meta = json.loads(out[1].text)["discoveryMeta"]
    assert meta == {
        "truncated": True,
        "total": 5,
        "maxMatches": 2,
        "degradedServers": [],
    }


# -- searchToolsForDomain handler ----------------------------------------------------


async def test_searchtoolsfordomain_carries_degraded(tmp_path, monkeypatch) -> None:
    handler = _build(
        tmp_path,
        monkeypatch,
        [_server("ok"), _server("flaky")],
        {"ok": [_tool("alpha")]},
    )
    out = await handler("searchToolsForDomain", {"domain": "default", "query": "alpha"})
    payload = json.loads(out[0].text)
    assert [d["serverId"] for d in payload["degradedServers"]] == ["flaky"]
    assert payload["pagination"]["total"] == 1  # only ok contributed tools
