"""PR 5.3: LiveMcpTracker bounded memory, collision-free call ids, shielded end.

On main: ``_clients`` grew one entry per session id forever (snapshot only
*hid* idle sessions), ``tool_name:<ms>`` call ids collided for concurrent
same-millisecond calls, and a cancelled tool task never reached
``end_tool_call`` (its lock acquire re-raised Cancelled), leaking
active-call entries.
"""

from __future__ import annotations

import time

import anyio
import pytest

import mcp_proxy.live_mcp_tracker as lmt
from mcp_proxy.live_mcp_tracker import LiveMcpTracker, live_tool_span


class FakeClock:
    """Starts at the real wall clock so recent_calls age-filters behave."""

    def __init__(self) -> None:
        self.ms = int(time.time() * 1000)

    def __call__(self) -> int:
        return self.ms

    def advance(self, ms: int) -> None:
        self.ms += ms


@pytest.fixture()
def clock(monkeypatch) -> FakeClock:
    c = FakeClock()
    monkeypatch.setattr(lmt, "_now_ms", c)
    return c


async def _touch(tracker: LiveMcpTracker, sid: str) -> None:
    await tracker.touch(
        session_id=sid,
        peer=None,
        user_agent=None,
        api_client_id=None,
        api_client_label=None,
    )


async def test_concurrent_same_ms_calls_get_unique_ids(clock: FakeClock) -> None:
    tracker = LiveMcpTracker()
    await _touch(tracker, "s1")
    k1 = await tracker.begin_tool_call(session_id="s1", tool_name="x", arguments={})
    k2 = await tracker.begin_tool_call(session_id="s1", tool_name="x", arguments={})
    assert k1 != k2  # old code: f"x:{now}" twice → one entry clobbered the other
    await tracker.end_tool_call(session_id="s1", call_id=k1)
    await tracker.end_tool_call(session_id="s1", call_id=k2)
    snap = await tracker.snapshot()
    client = next(c for c in snap["clients"] if c["session_id"] == "s1")
    assert client["active_calls"] == []
    assert len(client["recent_calls"]) == 2  # old code: only one survived


async def test_snapshot_evicts_idle_sessions(clock: FakeClock) -> None:
    tracker = LiveMcpTracker()
    await _touch(tracker, "dead")
    clock.advance(91_000)
    await _touch(tracker, "alive")
    snap = await tracker.snapshot(active_within_ms=90_000)
    assert [c["session_id"] for c in snap["clients"]] == ["alive"]
    assert "dead" not in tracker._clients  # removed, not just hidden


async def test_idle_session_with_young_active_call_survives(clock: FakeClock) -> None:
    tracker = LiveMcpTracker()
    await _touch(tracker, "s1")
    await tracker.begin_tool_call(session_id="s1", tool_name="long", arguments={})
    clock.advance(91_000)  # session idle past horizon, but the call is running
    await tracker.snapshot(active_within_ms=90_000)
    assert "s1" in tracker._clients
    # Simulate a crash that leaked the call entry: past the stale-call horizon
    # even the call is dropped, and with it the session.
    clock.advance(LiveMcpTracker._STALE_CALL_MS + 1)
    await tracker.snapshot(active_within_ms=90_000)
    assert "s1" not in tracker._clients


async def test_end_tool_call_survives_cancellation() -> None:
    tracker = LiveMcpTracker()
    lmt.current_mcp_session_id.set("s1")
    with anyio.move_on_after(0.05):
        async with live_tool_span(tracker, tool_name="boom", arguments=None):
            await anyio.sleep(30)  # cancelled by the scope (client disconnect)
    snap = await tracker.snapshot()
    client = next(c for c in snap["clients"] if c["session_id"] == "s1")
    assert client["active_calls"] == []  # ← leaked on main (no shield)
    assert len(client["recent_calls"]) == 1
