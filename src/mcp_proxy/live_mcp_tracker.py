"""Live view of connected MCP clients and in-flight tool calls (admin diagnostics)."""

from __future__ import annotations

import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

import anyio


current_mcp_session_id: ContextVar[str | None] = ContextVar(
    "current_mcp_session_id", default=None
)
current_mcp_peer: ContextVar[str | None] = ContextVar("current_mcp_peer", default=None)
current_mcp_user_agent: ContextVar[str | None] = ContextVar(
    "current_mcp_user_agent", default=None
)
current_mcp_api_client_id: ContextVar[str | None] = ContextVar(
    "current_mcp_api_client_id", default=None
)
current_mcp_api_client_label: ContextVar[str | None] = ContextVar(
    "current_mcp_api_client_label", default=None
)


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(slots=True)
class LiveToolCall:
    tool_name: str
    started_at_ms: int
    arguments_preview: str | None = None


@dataclass(slots=True)
class LiveMcpClient:
    session_id: str
    peer: str | None = None
    user_agent: str | None = None
    api_client_id: str | None = None
    api_client_label: str | None = None
    first_seen_at_ms: int = field(default_factory=_now_ms)
    last_seen_at_ms: int = field(default_factory=_now_ms)
    active_calls: dict[str, LiveToolCall] = field(default_factory=dict)


class LiveMcpTracker:
    """In-memory tracker for MCP sessions and running tool calls.

    This is best-effort diagnostic state. It resets on process restart.
    """

    def __init__(self) -> None:
        self._lock = anyio.Lock()
        self._clients: dict[str, LiveMcpClient] = {}

    async def touch(
        self,
        *,
        session_id: str,
        peer: str | None,
        user_agent: str | None,
        api_client_id: str | None,
        api_client_label: str | None,
    ) -> None:
        now = _now_ms()
        async with self._lock:
            c = self._clients.get(session_id)
            if c is None:
                c = LiveMcpClient(
                    session_id=session_id,
                    peer=peer,
                    user_agent=user_agent,
                    api_client_id=api_client_id,
                    api_client_label=api_client_label,
                    first_seen_at_ms=now,
                    last_seen_at_ms=now,
                )
                self._clients[session_id] = c
                return
            c.last_seen_at_ms = now
            if peer:
                c.peer = peer
            if user_agent:
                c.user_agent = user_agent
            if api_client_id:
                c.api_client_id = api_client_id
            if api_client_label:
                c.api_client_label = api_client_label

    async def begin_tool_call(
        self, *, session_id: str, tool_name: str, arguments: dict[str, Any] | None
    ) -> str:
        now = _now_ms()
        key = f"{tool_name}:{now}"
        preview: str | None = None
        if arguments:
            try:
                # Keep this small; we only want to indicate what's being called.
                preview = str(list(arguments.keys())[:16])
            except Exception:
                preview = None
        async with self._lock:
            c = self._clients.get(session_id)
            if c is None:
                c = LiveMcpClient(session_id=session_id)
                self._clients[session_id] = c
            c.last_seen_at_ms = now
            c.active_calls[key] = LiveToolCall(
                tool_name=tool_name, started_at_ms=now, arguments_preview=preview
            )
        return key

    async def end_tool_call(self, *, session_id: str, call_id: str) -> None:
        async with self._lock:
            c = self._clients.get(session_id)
            if c is None:
                return
            c.active_calls.pop(call_id, None)

    async def snapshot(self, *, active_within_ms: int = 90_000) -> dict[str, Any]:
        """Return a JSON-serializable snapshot for the admin UI."""
        now = _now_ms()
        cutoff = now - max(1, int(active_within_ms))
        async with self._lock:
            out: list[dict[str, Any]] = []
            for c in self._clients.values():
                if c.last_seen_at_ms < cutoff:
                    continue
                calls = []
                for cid, call in c.active_calls.items():
                    calls.append(
                        {
                            "id": cid,
                            "tool": call.tool_name,
                            "started_at_ms": call.started_at_ms,
                            "running_ms": max(0, now - call.started_at_ms),
                            "arguments_preview": call.arguments_preview,
                        }
                    )
                calls.sort(key=lambda x: int(x["started_at_ms"]))
                out.append(
                    {
                        "session_id": c.session_id,
                        "peer": c.peer,
                        "user_agent": c.user_agent,
                        "api_client_id": c.api_client_id,
                        "api_client_label": c.api_client_label,
                        "first_seen_at_ms": c.first_seen_at_ms,
                        "last_seen_at_ms": c.last_seen_at_ms,
                        "idle_ms": max(0, now - c.last_seen_at_ms),
                        "active_calls": calls,
                    }
                )
        out.sort(key=lambda x: int(x["last_seen_at_ms"]), reverse=True)
        return {"now_ms": now, "clients": out}
