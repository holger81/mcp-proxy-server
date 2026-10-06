"""ASGI middleware: track live /mcp sessions and bind request contextvars."""

from __future__ import annotations

import logging
from typing import Callable

from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from mcp_proxy.live_mcp_tracker import (
    LiveMcpTracker,
    current_mcp_api_client,
    current_mcp_api_client_id,
    current_mcp_api_client_label,
    current_mcp_peer,
    current_mcp_session_id,
    current_mcp_user_agent,
    update_session_identity,
)

logger = logging.getLogger("mcp_proxy.auth")


def _bearer_from_headers(hdrs: dict[str, str]) -> str | None:
    auth = hdrs.get("authorization") or ""
    if not auth.startswith("Bearer "):
        return None
    tok = auth[7:].strip()
    return tok or None


def _is_mcp_http_path(path: str) -> bool:
    return path == "/mcp" or path.startswith("/mcp/")


def _header_map(scope: Scope) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw_k, raw_v in scope.get("headers") or []:
        try:
            k = raw_k.decode("latin-1").lower()
            v = raw_v.decode("latin-1", errors="replace")
        except Exception:
            continue
        out[k] = v
    return out


class McpLiveTrackerMiddleware:
    """Track active MCP clients (best-effort).

    - Sets contextvars with session + auth info so `callTool` can associate work to a client.
    - Touches the in-memory tracker on every /mcp HTTP exchange.
    """

    def __init__(self, app: Callable, tracker: LiveMcpTracker, client_store) -> None:
        self.app = app
        self.tracker = tracker
        self.client_store = client_store

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or not _is_mcp_http_path(
            scope.get("path") or ""
        ):
            await self.app(scope, receive, send)
            return

        client = scope.get("client")
        peer = (
            f"{client[0]}:{client[1]}"
            if client and isinstance(client, (list, tuple)) and len(client) >= 2
            else None
        )
        hdrs = _header_map(scope)
        sess = (hdrs.get("mcp-session-id") or "").strip() or None
        ua = (hdrs.get("user-agent") or "").strip() or None

        api_client_id = None
        api_client_label = None
        api_client_rec = None
        token = _bearer_from_headers(hdrs)
        if token and self.client_store is not None:
            # Fail closed (PLAN 4.3): if the store cannot answer who this
            # token belongs to, reject instead of serving the request with
            # an anonymous (policy-free) context. Retry once first — these
            # errors are usually transient file/IO hiccups.
            for attempt in (1, 2):
                try:
                    rec = self.client_store.resolve_bearer(token)
                    if rec is not None:
                        api_client_rec = rec
                        api_client_id = rec.id
                        api_client_label = rec.label
                    break
                except Exception:  # noqa: BLE001 - any store failure
                    if attempt == 1:
                        continue
                    logger.exception(
                        "client store failed to resolve bearer token for /mcp "
                        "request; rejecting (fail closed)"
                    )
                    response = JSONResponse(
                        {"detail": "Authentication backend unavailable. Try again."},
                        status_code=401,
                    )
                    return await response(scope, receive, send)

        tok_sess = current_mcp_session_id.set(sess)
        tok_peer = current_mcp_peer.set(peer)
        tok_ua = current_mcp_user_agent.set(ua)
        tok_client = current_mcp_api_client.set(api_client_rec)
        tok_cid = current_mcp_api_client_id.set(api_client_id)
        tok_clabel = current_mcp_api_client_label.set(api_client_label)
        if sess:
            # Stateful session handlers run in the session's own task and
            # cannot see the ContextVars set here; hand them the freshly
            # resolved client via the per-session slot (PR 5.8).
            update_session_identity(sess, api_client_rec)
        try:
            if sess:
                await self.tracker.touch(
                    session_id=sess,
                    peer=peer,
                    user_agent=ua,
                    api_client_id=api_client_id,
                    api_client_label=api_client_label,
                )
            await self.app(scope, receive, send)
        finally:
            current_mcp_session_id.reset(tok_sess)
            current_mcp_peer.reset(tok_peer)
            current_mcp_user_agent.reset(tok_ua)
            current_mcp_api_client.reset(tok_client)
            current_mcp_api_client_id.reset(tok_cid)
            current_mcp_api_client_label.reset(tok_clabel)
