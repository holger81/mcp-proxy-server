"""PLAN 6.2: per-server tool-list TTL cache + concurrent discovery fan-out."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import anyio
import pytest
from mcp import types as mcp_types

import mcp_proxy.proxy_mcp as pm
from mcp_proxy.config_store import ServerConfigStore
from mcp_proxy.models import UpstreamServer
from mcp_proxy.settings import Settings
from mcp_proxy.tool_call_stats import ToolCallStatsStore


def _tool(name: str) -> mcp_types.Tool:
    return mcp_types.Tool(
        name=name, description="d", inputSchema={"type": "object", "properties": {}}
    )


def _server(sid: str, domain: str = "default") -> SimpleNamespace:
    return SimpleNamespace(id=sid, domain=domain, enabled=True, llm_context=None)


class FakeStore:
    def __init__(self, servers) -> None:
        self._servers = {s.id: s for s in servers}

    def list_servers(self):
        return list(self._servers.values())

    def get(self, sid: str):
        return self._servers.get(sid)


def _install_fake(monkeypatch, tool_map, *, sleeps=0.0) -> list[str]:
    """Replace `_list_upstream_tools`; records which server ids were fetched."""
    fetched: list[str] = []

    async def fake(upstream, _settings):
        fetched.append(upstream.id)
        if sleeps:
            await anyio.sleep(sleeps)
        if upstream.id not in tool_map:
            raise ConnectionError(f"{upstream.id} down")
        return tool_map[upstream.id]

    monkeypatch.setattr(pm, "_list_upstream_tools", fake)
    return fetched


def _build(tmp_path, servers, *, settings=None, tool_cache=None):
    settings = settings or Settings(data_dir=tmp_path)
    stats = ToolCallStatsStore(tmp_path, flush_interval_s=3600.0)
    domain_store = SimpleNamespace(list_records=lambda: [])
    return pm.build_proxy_mcp_server(
        FakeStore(servers),
        domain_store,
        settings,
        stats,
        tool_list_cache=tool_cache,
    )


def _caller(srv):
    request_handler = srv.request_handlers[mcp_types.CallToolRequest]

    async def call(name: str, arguments: dict):
        req = mcp_types.CallToolRequest(
            method="tools/call",
            params=mcp_types.CallToolRequestParams(name=name, arguments=arguments),
        )
        result = (await request_handler(req)).root
        return result

    return call


# --- TTL behavior -----------------------------------------------------------


async def test_repeat_discovery_uses_cache(tmp_path, monkeypatch):
    srv = _build(tmp_path, [_server("ok")])
    fetched = _install_fake(monkeypatch, {"ok": [_tool("alpha")]})
    call = _caller(srv)
    for _ in range(3):
        result = await call("searchToolsForDomain", {"domain": "default", "query": "alpha"})
        assert not result.isError
    assert fetched == ["ok"]


async def test_ttl_zero_disables_cache(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path, tool_list_cache_ttl_s=0.0)
    srv = _build(tmp_path, [_server("ok")], settings=settings)
    fetched = _install_fake(monkeypatch, {"ok": [_tool("alpha")]})
    call = _caller(srv)
    for _ in range(2):
        await call("searchToolsForDomain", {"domain": "default", "query": "alpha"})
    assert fetched == ["ok", "ok"]


async def test_expiry_triggers_refetch(tmp_path, monkeypatch):
    from mcp_proxy.upstream_tool_cache import UpstreamToolListCache
    cache = UpstreamToolListCache(ttl_s=0.05)
    srv = _build(tmp_path, [_server("ok")], tool_cache=cache)
    fetched = _install_fake(monkeypatch, {"ok": [_tool("alpha")]})
    call = _caller(srv)
    await call("searchToolsForDomain", {"domain": "default", "query": "alpha"})
    await call("searchToolsForDomain", {"domain": "default", "query": "alpha"})
    assert fetched == ["ok"]
    await anyio.sleep(0.1)
    await call("searchToolsForDomain", {"domain": "default", "query": "alpha"})
    assert fetched == ["ok", "ok"]


# --- invalidation ------------------------------------------------------------


async def test_config_edit_invalidates_entry(tmp_path, monkeypatch):
    from mcp_proxy.upstream_tool_cache import UpstreamToolListCache
    store = ServerConfigStore(tmp_path)
    store.add(UpstreamServer(id="ok", domain="default", type="stdio", command=["a"]))
    settings = Settings(data_dir=tmp_path)
    cache = UpstreamToolListCache(ttl_s=3600)
    stats = ToolCallStatsStore(tmp_path, flush_interval_s=3600.0)
    srv = pm.build_proxy_mcp_server(
        store,
        SimpleNamespace(list_records=lambda: []),
        settings,
        stats,
        tool_list_cache=cache,
    )
    fetched = _install_fake(monkeypatch, {"ok": [_tool("alpha")]})
    call = _caller(srv)
    await call("searchToolsForDomain", {"domain": "default", "query": "alpha"})
    await call("searchToolsForDomain", {"domain": "default", "query": "alpha"})
    assert fetched == ["ok"]
    # Simulate an admin repointing the command: fingerprint changes -> refetch.
    store.update_fields(
        "ok", lambda s: s.model_copy(update={"command": ["b"]})
    )
    await call("searchToolsForDomain", {"domain": "default", "query": "alpha"})
    assert fetched == ["ok", "ok"]


async def test_fetch_error_invalidates_and_recovery_is_visible(
    tmp_path, monkeypatch
):
    from mcp_proxy.upstream_tool_cache import UpstreamToolListCache
    # Short TTL so the cached success expires; the failed refetch must then
    # invalidate (not cache the failure) so recovery is visible immediately.
    cache = UpstreamToolListCache(ttl_s=0.05)
    srv = _build(tmp_path, [_server("ok")], tool_cache=cache)
    state = {"down": False}
    fetched: list[str] = []

    async def fake(upstream, _settings):
        fetched.append(upstream.id)
        if state["down"]:
            raise ConnectionError("boom")
        return [_tool("alpha")]

    monkeypatch.setattr(pm, "_list_upstream_tools", fake)
    call = _caller(srv)
    r1 = await call("searchToolsForDomain", {"domain": "default", "query": "alpha"})
    assert not r1.isError
    await anyio.sleep(0.08)
    state["down"] = True
    r2 = await call("searchToolsForDomain", {"domain": "default", "query": "alpha"})
    payload = json.loads(r2.content[0].text)  # degraded surfaced
    assert payload["degradedServers"] == [{"serverId": "ok", "error": "boom"}]
    state["down"] = False
    # No sleep: if the error had been cached, this would still report degraded.
    r3 = await call("searchToolsForDomain", {"domain": "default", "query": "alpha"})
    payload = json.loads(r3.content[0].text)
    assert payload["tools"] and not payload["degradedServers"]
    assert fetched == ["ok", "ok", "ok"]


# --- concurrent fan-out -------------------------------------------------------


async def test_fanout_is_concurrent(tmp_path, monkeypatch):
    servers = [_server(f"s{i}") for i in range(4)]
    srv = _build(tmp_path, servers)
    _install_fake(
        monkeypatch,
        {f"s{i}": [_tool(f"t{i}")] for i in range(4)},
        sleeps=0.2,
    )
    call = _caller(srv)
    t0 = time.monotonic()
    result = await call("searchToolsForDomain", {"domain": "default", "query": "t"})
    dt = time.monotonic() - t0
    assert not result.isError
    payload = json.loads(result.content[0].text)
    assert len(payload["tools"]) == 4
    assert not payload["degradedServers"]
    # Sequential discovery would need >= 0.8 s; concurrent needs ~0.2 s.
    assert dt < 0.5, f"fan-out looks sequential: {dt:.2f}s"


async def test_shared_deadline_keeps_fast_results(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path, upstream_timeout_s=5.0)
    srv = _build(tmp_path, [_server("fast"), _server("hang")], settings=settings)

    async def fake(upstream, _settings):
        if upstream.id == "hang":
            await anyio.sleep(60)
        return [_tool("alpha")]

    monkeypatch.setattr(pm, "_list_upstream_tools", fake)
    call = _caller(srv)
    t0 = time.monotonic()
    result = await call("searchToolsForDomain", {"domain": "default", "query": "alpha"})
    dt = time.monotonic() - t0
    assert not result.isError
    payload = json.loads(result.content[0].text)
    assert any("fast" in r["toolName"] for r in payload["tools"])  # partials survive
    assert payload["degradedServers"] == [{"serverId": "hang", "error": "timeout"}]
    assert 4.5 < dt < 12, f"shared deadline not honored: {dt:.2f}s"


# --- cache unit tests ----------------------------------------------------------


def test_fingerprint_tracks_config():
    from mcp_proxy.upstream_tool_cache import server_fingerprint
    a = UpstreamServer(id="x", type="stdio", command=["a"])
    b = UpstreamServer(id="x", type="stdio", command=["a"])
    c = a.model_copy(update={"command": ["b"]})
    assert server_fingerprint(a) == server_fingerprint(b)
    assert server_fingerprint(a) != server_fingerprint(c)


def test_cache_evicts_oldest_beyond_capacity():
    from mcp_proxy.upstream_tool_cache import UpstreamToolListCache
    cache = UpstreamToolListCache(ttl_s=60, max_entries=2)
    s1, s2, s3 = (
        UpstreamServer(id=f"s{i}", type="stdio", command=["x"]) for i in (1, 2, 3)
    )
    cache.put(s1, [_tool("a")])
    time.sleep(0.01)
    cache.put(s2, [_tool("b")])
    cache.put(s3, [_tool("c")])
    assert cache.get(s1) is None  # evicted
    assert cache.get(s2) is not None
    assert cache.get(s3) is not None


def test_invalidate_specific_and_all():
    from mcp_proxy.upstream_tool_cache import UpstreamToolListCache
    cache = UpstreamToolListCache(ttl_s=60)
    s1 = UpstreamServer(id="s1", type="stdio", command=["x"])
    s2 = UpstreamServer(id="s2", type="stdio", command=["x"])
    cache.put(s1, [_tool("a")])
    cache.put(s2, [_tool("b")])
    cache.invalidate("s1")
    assert cache.get(s1) is None
    assert cache.get(s2) is not None
    cache.invalidate()
    assert cache.get(s2) is None


def test_disabled_cache_stores_and_serves_nothing():
    from mcp_proxy.upstream_tool_cache import UpstreamToolListCache
    cache = UpstreamToolListCache(ttl_s=0)
    s = UpstreamServer(id="s", type="stdio", command=["x"])
    cache.put(s, [_tool("a")])
    assert cache.get(s) is None


async def test_admin_catalog_shares_mcp_cache(tmp_path, monkeypatch):
    from mcp_proxy.upstream_tool_cache import UpstreamToolListCache
    cache = UpstreamToolListCache(ttl_s=3600)
    srv = _build(tmp_path, [_server("ok")], tool_cache=cache)
    fetched = _install_fake(monkeypatch, {"ok": [_tool("alpha")]})
    call = _caller(srv)
    await call("searchToolsForDomain", {"domain": "default", "query": "alpha"})
    settings = Settings(data_dir=tmp_path)
    await pm.build_tool_catalog_for_admin(
        FakeStore([_server("ok")]), settings, cache
    )
    assert fetched == ["ok"]  # second discovery served from the shared cache
