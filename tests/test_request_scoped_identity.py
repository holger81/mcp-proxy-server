"""PR 5.8: stateful MCP sessions must re-resolve the client per request.

Streamable HTTP runs the MCP session loop (and every tool handler) in a
long-lived background task. Its ContextVars — including
``current_mcp_api_client`` set by the auth middleware — are snapshotted when
the task is spawned during ``initialize``, so on main a live session enforced
the *creator's* policies forever: a second client on the same session got the
creator's tool visibility, and token revocation or per-client policy edits
never reached the running session.

The fix: a per-session mutable identity slot (``live_mcp_tracker``), opened by
the session manager around the session loop and updated by the middleware
with the freshly resolved bearer of every HTTP request; handlers read it via
``resolve_current_api_client()``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from mcp_proxy.client_store import ApiClientRecord
from mcp_proxy.live_mcp_tracker import (
    LiveMcpTracker,
    current_mcp_api_client,
)
from mcp_proxy.live_streamable_http_session_manager import (
    LiveBindingStreamableHTTPSessionManager,
)
from mcp_proxy.mcp_live_tracker_middleware import McpLiveTrackerMiddleware
from mcp_proxy.proxy_mcp import build_proxy_mcp_server
from mcp_proxy.settings import Settings
from mcp_proxy.tool_call_stats import ToolCallStatsStore

_HDRS = {
    "content-type": "application/json",
    "accept": "application/json, text/event-stream",
}


def _record(cid: str, disabled: list[str] | None = None) -> ApiClientRecord:
    return ApiClientRecord(
        id=cid,
        label=cid,
        created_at="2026-01-01T00:00:00Z",
        token_sha256_hex="0" * 64,
        disabled_tools=disabled or [],
    )


class _ClientStore:
    """Minimal bearer resolver; tests swap records to simulate store edits."""

    def __init__(self) -> None:
        self.by_token: dict[str, ApiClientRecord | None] = {}

    def resolve_bearer(self, token: str):
        return self.by_token.get(token)

    def verify_bearer(self, token: str) -> bool:
        return token in self.by_token


def _stateful_app(tmp_path, cstore: _ClientStore) -> TestClient:
    settings = Settings(data_dir=tmp_path)
    srv = build_proxy_mcp_server(
        SimpleNamespace(list_servers=lambda: [], get=lambda sid: None),
        SimpleNamespace(list_records=lambda: []),
        settings,
        ToolCallStatsStore(tmp_path),
    )
    manager = LiveBindingStreamableHTTPSessionManager(srv, stateless=False)

    class _App:
        def __init__(self) -> None:
            self._cm = None

        async def __call__(self, scope, receive, send) -> None:
            if scope["type"] == "lifespan":
                while True:
                    msg = await receive()
                    if msg["type"] == "lifespan.startup":
                        self._cm = manager.run()
                        await self._cm.__aenter__()
                        await send({"type": "lifespan.startup.complete"})
                    elif msg["type"] == "lifespan.shutdown":
                        await self._cm.__aexit__(None, None, None)
                        await send({"type": "lifespan.shutdown.complete"})
                        return
            else:
                await manager.handle_request(scope, receive, send)

    return TestClient(
        McpLiveTrackerMiddleware(_App(), LiveMcpTracker(), cstore),
        base_url="http://testserver",
    )


def _sse_data(body: str) -> dict:
    for line in body.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    raise AssertionError(f"no SSE data payload in: {body!r}")


def _initialize(client: TestClient, token: str) -> str:
    r = client.post(
        "/mcp",
        headers={**_HDRS, "authorization": f"Bearer {token}"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "probe", "version": "0"},
            },
        },
    )
    assert r.status_code == 200, r.text
    sid = r.headers.get("mcp-session-id")
    assert sid
    client.post(
        "/mcp",
        headers={
            **_HDRS,
            "authorization": f"Bearer {token}",
            "mcp-session-id": sid,
        },
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    return sid


def _tool_names(client: TestClient, sid: str, token: str) -> list[str]:
    r = client.post(
        "/mcp",
        headers={
            **_HDRS,
            "authorization": f"Bearer {token}",
            "mcp-session-id": sid,
            "mcp-protocol-version": "2025-06-18",
        },
        json={"jsonrpc": "2.0", "id": 9, "method": "tools/list"},
    )
    payload = _sse_data(r.text)
    assert "result" in payload, payload
    return [t["name"] for t in payload["result"]["tools"]]


def test_stateful_session_policy_follows_requesting_client(tmp_path):
    """A second token on a live session gets its OWN tool policy (fails main)."""
    cstore = _ClientStore()
    cstore.by_token["tokA"] = _record("A")
    cstore.by_token["tokB"] = _record("B", disabled=["callTool"])

    client = _stateful_app(tmp_path, cstore)
    with client:
        sid = _initialize(client, "tokA")
        assert "callTool" in _tool_names(client, sid, "tokA")
        # Same session id, different bearer: main pinned this to A's record.
        assert "callTool" not in _tool_names(client, sid, "tokB")
        assert "callTool" in _tool_names(client, sid, "tokA")


def test_policy_change_applies_on_next_call(tmp_path):
    """Store edits (fresh record object, like a disk reload) are not stale."""
    cstore = _ClientStore()
    cstore.by_token["tokA"] = _record("A")

    client = _stateful_app(tmp_path, cstore)
    with client:
        sid = _initialize(client, "tokA")
        assert "searchTool" in _tool_names(client, sid, "tokA")

        cstore.by_token["tokA"] = _record("A", disabled=["searchTool"])
        assert "searchTool" not in _tool_names(client, sid, "tokA")

        cstore.by_token["tokA"] = _record("A")
        assert "searchTool" in _tool_names(client, sid, "tokA")


def test_revoked_token_rejected_on_next_call(tmp_path):
    """Revocation fails closed on the next /mcp request (no anonymous bind).

    A Bearer that no longer resolves must not continue unbound — that used to
    fail-open the /mcp admin-tool gate. Expect HTTP 401 instead.
    """
    cstore = _ClientStore()
    cstore.by_token["tokA"] = _record("A", disabled=["callTool"])

    client = _stateful_app(tmp_path, cstore)
    with client:
        sid = _initialize(client, "tokA")
        assert "callTool" not in _tool_names(client, sid, "tokA")

        cstore.by_token["tokA"] = None  # revoked
        r = client.post(
            "/mcp",
            headers={
                "Authorization": "Bearer tokA",
                "mcp-session-id": sid,
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {},
            },
        )
        assert r.status_code == 401
        assert "revoked" in r.json()["detail"].lower() or "invalid" in r.json()["detail"].lower()


def test_identity_slot_seeding_update_and_cleanup():
    from mcp_proxy.live_mcp_tracker import (
        resolve_current_api_client,
        session_identity,
        session_identity_scope,
        update_session_identity,
    )

    rec = _record("seed")
    tok = current_mcp_api_client.set(rec)
    try:
        with session_identity_scope("s1") as ident:
            # Seeded from the initializing request's ContextVar.
            assert ident.client is rec
            assert resolve_current_api_client() is rec

            update_session_identity("s1", None)
            assert resolve_current_api_client() is None
            update_session_identity("s1", rec)
            assert session_identity("s1") is ident
            update_session_identity("no-such-session", rec)  # no-op

        assert session_identity("s1") is None
        # Outside a session loop the per-request ContextVar is authoritative.
        assert resolve_current_api_client() is rec
    finally:
        current_mcp_api_client.reset(tok)


def test_identity_slot_dropped_when_session_ends(tmp_path):
    from mcp_proxy.live_mcp_tracker import session_identity

    cstore = _ClientStore()
    cstore.by_token["tokA"] = _record("A")

    client = _stateful_app(tmp_path, cstore)
    with client:
        sid = _initialize(client, "tokA")
        assert session_identity(sid) is not None
    assert session_identity(sid) is None
