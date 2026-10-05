"""PR 5.4: stats persistence is debounced write-behind; prune only on verified misses.

On main: every successful callTool rewrote the whole stats JSON synchronously,
and a *transient* upstream failure (timeout/transport) looked identical to
"tool removed upstream", so hot-tool shortcuts were pruned by flakiness.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from mcp import types as mcp_types

import mcp_proxy.proxy_mcp as pm
from mcp_proxy.proxy_mcp import ToolRowOutcome, _verified_hot_tools
from mcp_proxy.settings import Settings
from mcp_proxy.tool_call_stats import ToolCallStatsStore


def _wait_file(store: ToolCallStatsStore, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if store._path.exists():
            return True
        time.sleep(0.01)
    return False


# -- write-behind ----------------------------------------------------------------


def test_record_defers_write_then_flushes(tmp_path) -> None:
    store = ToolCallStatsStore(tmp_path, flush_interval_s=0.05)
    store.record_success("srv__tool")
    assert not store._path.exists()  # not synchronous anymore
    assert _wait_file(store)
    assert "srv__tool" in store._path.read_text()


def test_many_records_coalesce_into_few_writes(tmp_path) -> None:
    store = ToolCallStatsStore(tmp_path, flush_interval_s=0.05)
    writes = 0

    real = store._persist_unlocked

    def counting():
        nonlocal writes
        writes += 1
        real()

    store._persist_unlocked = counting  # type: ignore[method-assign]
    for i in range(50):
        store.record_success(f"srv__tool{i % 5}")
    store.flush()
    assert writes <= 2  # old code: 50 writes
    assert store.ranked_keys()  # still correct in memory


def test_flush_writes_pending_and_is_idempotent(tmp_path) -> None:
    store = ToolCallStatsStore(tmp_path, flush_interval_s=3600.0)
    store.record_success("a__t")
    store.flush()
    first = store._path.read_text()
    store.flush()  # nothing dirty → no rewrite
    assert store._path.read_text() == first
    store2 = ToolCallStatsStore(tmp_path, flush_interval_s=3600.0)
    assert store2.ranked_keys() == ["a__t"]  # round-trip


def test_key_count_is_capped(tmp_path) -> None:
    store = ToolCallStatsStore(tmp_path, flush_interval_s=3600.0, max_keys=2)
    for _ in range(5):
        store.record_success("a__t")
    for _ in range(3):
        store.record_success("b__t")
    store.record_success("c__t")  # least popular → evicted on insert
    assert store.ranked_keys() == ["a__t", "b__t"]


def test_load_applies_cap(tmp_path) -> None:
    store = ToolCallStatsStore(tmp_path, flush_interval_s=3600.0)
    store.record_success("a__t")
    store.record_success("b__t")
    store.record_success("c__t")
    store.flush()
    reloaded = ToolCallStatsStore(tmp_path, flush_interval_s=3600.0, max_keys=2)
    assert len(reloaded.ranked_keys()) == 2


# -- tri-state hot-tool pruning ---------------------------------------------------


UPSTREAM = SimpleNamespace(id="fake", domain="default", enabled=True, llm_context=None)


class FakeStore:
    def get(self, sid: str):
        return UPSTREAM if sid == "fake" else None


@pytest.fixture()
def hot_setup(tmp_path):
    settings = Settings()
    stats = ToolCallStatsStore(tmp_path, flush_interval_s=3600.0)
    stats.record_success("fake__tool")
    return settings, stats


def _tool(name: str) -> mcp_types.Tool:
    return mcp_types.Tool(
        name=name, description="d", inputSchema={"type": "object", "properties": {}}
    )


async def test_transient_error_does_not_prune(hot_setup, monkeypatch) -> None:
    settings, stats = hot_setup

    async def boom(upstream, _settings):
        raise ConnectionError("upstream down")

    monkeypatch.setattr(pm, "_list_upstream_tools", boom)
    out = await _verified_hot_tools(FakeStore(), settings, stats, frozenset())
    assert out == []
    assert stats.ranked_keys() == ["fake__tool"]  # ← main would have pruned it


async def test_disabled_server_still_prunes(hot_setup) -> None:
    settings, stats = hot_setup
    disabled_store = SimpleNamespace(
        get=lambda sid: SimpleNamespace(id="fake", enabled=False)
    )
    out = await _verified_hot_tools(disabled_store, settings, stats, frozenset())
    assert out == []
    assert stats.ranked_keys() == []  # verified gone → prune stays


async def test_verified_tool_survives_and_is_listed(hot_setup, monkeypatch) -> None:
    settings, stats = hot_setup

    async def ok(upstream, _settings):
        return [_tool("tool")]

    monkeypatch.setattr(pm, "_list_upstream_tools", ok)
    out = await _verified_hot_tools(FakeStore(), settings, stats, frozenset())
    assert [t.name for t in out] == ["fake__tool"]
    assert stats.ranked_keys() == ["fake__tool"]


async def test_lookup_returns_error_outcome(hot_setup, monkeypatch) -> None:
    settings, _ = hot_setup

    async def boom(upstream, _settings):
        raise TimeoutError()

    monkeypatch.setattr(pm, "_list_upstream_tools", boom)
    outcome, row = await pm._lookup_tool_row(FakeStore(), settings, "fake__tool")
    assert outcome is ToolRowOutcome.ERROR
    assert row is None
