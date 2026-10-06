"""Live view of connected MCP clients and in-flight tool calls (admin diagnostics)."""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import anyio

if TYPE_CHECKING:
    from mcp_proxy.client_store import ApiClientRecord


current_mcp_session_id: ContextVar[str | None] = ContextVar(
    "current_mcp_session_id", default=None
)
current_mcp_api_client: ContextVar[ApiClientRecord | None] = ContextVar(
    "current_mcp_api_client", default=None
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


@dataclass(slots=True)
class SessionIdentity:
    """Mutable identity slot for one live *stateful* MCP session (PR 5.8).

    Streamable HTTP runs the MCP session loop (and therefore every tool
    handler) in a long-lived background task whose ContextVars are snapshotted
    when the session is created, so the middleware's per-request
    ``current_mcp_api_client`` never reaches handlers of an existing session.
    The manager registers one of these slots per session; the middleware
    updates ``client`` from the *current* request's resolved bearer before each
    request is dispatched, so token revocation and per-client policy changes
    take effect on the next call instead of at re-initialize.
    """

    client: ApiClientRecord | None = None


# session id -> live slot (only for stateful sessions while they run)
_session_identities: dict[str, SessionIdentity] = {}

_current_session_identity: ContextVar[SessionIdentity | None] = ContextVar(
    "current_session_identity", default=None
)


@contextmanager
def session_identity_scope(session_id: str) -> Iterator[SessionIdentity]:
    """Bind a fresh identity slot for ``session_id`` around the session loop.

    Seed value is the *initializing* request's client (the caller's ContextVar
    is authoritative there); later requests update the slot in place.
    """
    ident = SessionIdentity(client=current_mcp_api_client.get())
    _session_identities[session_id] = ident
    tok = _current_session_identity.set(ident)
    try:
        yield ident
    finally:
        _current_session_identity.reset(tok)
        if _session_identities.get(session_id) is ident:
            del _session_identities[session_id]


def update_session_identity(
    session_id: str, client: ApiClientRecord | None
) -> None:
    """Record the identity of the HTTP request currently being dispatched."""
    ident = _session_identities.get(session_id)
    if ident is not None:
        ident.client = client


def session_identity(session_id: str) -> SessionIdentity | None:
    """Live identity slot for ``session_id`` (diagnostics/tests)."""
    return _session_identities.get(session_id)


def resolve_current_api_client() -> ApiClientRecord | None:
    """API client to enforce policy for *right now*.

    Inside a stateful MCP session loop this is the client of the HTTP request
    currently being served (updated per request, PR 5.8). Everywhere else —
    stateless mode, initialize-time computation in the worker task, stdio —
    the per-request ContextVar set by the middleware is already correct.
    """
    ident = _current_session_identity.get()
    if ident is not None:
        return ident.client
    return current_mcp_api_client.get()


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(slots=True)
class LiveToolCall:
    tool_name: str
    started_at_ms: int
    arguments_preview: str | None = None


@dataclass(slots=True)
class LiveRecentCall:
    tool_name: str
    finished_at_ms: int
    duration_ms: int


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
    recent_calls: list[LiveRecentCall] = field(default_factory=list)


class LiveMcpTracker:
    """In-memory tracker for MCP sessions and running tool calls.

    This is best-effort diagnostic state. It resets on process restart.
    Memory is bounded via eviction in :meth:`snapshot` (the admin poll is the
    tracker's only periodic hook).
    """

    # Call entries younger than this keep their (idle) session alive in the
    # map; older ones can only be leaked bookkeeping (crash between
    # begin/end) and get dropped.
    _STALE_CALL_MS = 30 * 60_000

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
        # uuid suffix: two concurrent calls of the same tool in the same
        # millisecond must not share (and overwrite) one active_calls entry.
        key = f"{tool_name}:{now}:{uuid.uuid4().hex[:8]}"
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
        now = _now_ms()
        async with self._lock:
            c = self._clients.get(session_id)
            if c is None:
                return
            call = c.active_calls.pop(call_id, None)
            if call is not None:
                c.recent_calls.append(
                    LiveRecentCall(
                        tool_name=call.tool_name,
                        finished_at_ms=now,
                        duration_ms=max(0, now - call.started_at_ms),
                    )
                )
                cutoff = now - 120_000
                c.recent_calls = [
                    r for r in c.recent_calls if r.finished_at_ms >= cutoff
                ][-12:]

    async def latest_active_session_id(self, *, within_ms: int = 15_000) -> str | None:
        """Fallback when ContextVar session id is missing (tool runs off HTTP task)."""
        now = _now_ms()
        cutoff = now - max(1, int(within_ms))
        async with self._lock:
            best_id: str | None = None
            best_ts = 0
            for c in self._clients.values():
                if c.last_seen_at_ms >= cutoff and c.last_seen_at_ms >= best_ts:
                    best_ts = c.last_seen_at_ms
                    best_id = c.session_id
            return best_id

    async def snapshot(self, *, active_within_ms: int = 90_000) -> dict[str, Any]:
        """Return a JSON-serializable snapshot for the admin UI.

        Also evicts sessions idle beyond ``active_within_ms`` (unless they
        still hold a young active call) and call entries leaked by a crash
        between begin/end — without this, one entry per session id ever seen
        accumulated forever (PLAN 5.3).
        """
        now = _now_ms()
        cutoff = now - max(1, int(active_within_ms))
        async with self._lock:
            for sid in list(self._clients):
                c = self._clients[sid]
                if c.last_seen_at_ms >= cutoff:
                    continue
                if c.active_calls:
                    c.active_calls = {
                        k: call
                        for k, call in c.active_calls.items()
                        if now - call.started_at_ms < self._STALE_CALL_MS
                    }
                if not c.active_calls:
                    del self._clients[sid]
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
                recent = [
                    {
                        "tool": r.tool_name,
                        "finished_at_ms": r.finished_at_ms,
                        "duration_ms": r.duration_ms,
                        "ago_ms": max(0, now - r.finished_at_ms),
                    }
                    for r in c.recent_calls
                ]
                recent.sort(key=lambda x: int(x["finished_at_ms"]), reverse=True)
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
                        "recent_calls": recent,
                    }
                )
        out.sort(key=lambda x: int(x["last_seen_at_ms"]), reverse=True)
        return {"now_ms": now, "clients": out}


@asynccontextmanager
async def live_tool_span(
    tracker: LiveMcpTracker | None,
    *,
    tool_name: str,
    arguments: dict[str, Any] | None,
):
    """Track in-flight tool work for the admin Live clients panel."""
    if tracker is None:
        yield
        return
    session_id = current_mcp_session_id.get()
    if not session_id:
        session_id = await tracker.latest_active_session_id()
    if not session_id:
        yield
        return
    call_id = await tracker.begin_tool_call(
        session_id=session_id,
        tool_name=tool_name,
        arguments=arguments,
    )
    try:
        yield
    finally:
        # Runs on client-disconnect/timeout cancellation too: without the
        # shield, end_tool_call's lock acquire re-raises Cancelled and the
        # active-call entry leaks (PLAN 5.3).
        with anyio.CancelScope(shield=True):
            await tracker.end_tool_call(session_id=session_id, call_id=call_id)
