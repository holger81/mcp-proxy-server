"""Extra access context for /mcp (User-Agent, session hint, forwarded-for) in admin logs."""

from __future__ import annotations

import logging
from typing import Callable

from starlette.types import Receive, Scope, Send

log = logging.getLogger("mcp_proxy.mcp_client")


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


def _is_mcp_http_path(path: str) -> bool:
    return path == "/mcp" or path.startswith("/mcp/")


def _short(s: str, n: int) -> str:
    s = s.replace("\n", " ").replace("\r", " ").strip()
    if len(s) <= n:
        return s or "—"
    return s[: n - 1] + "…"


class McpClientAuditMiddleware:
    """Log one line per /mcp HTTP exchange: peer, forwarded chain, session id prefix, Origin, User-Agent, status."""

    def __init__(self, app: Callable) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or not _is_mcp_http_path(scope.get("path") or ""):
            await self.app(scope, receive, send)
            return

        client = scope.get("client")
        peer = f"{client[0]}:{client[1]}" if client and len(client) >= 2 else "—"
        hdrs = _header_map(scope)
        xff = hdrs.get("x-forwarded-for", "")
        if xff:
            xff = _short(xff.split(",")[0].strip(), 64)
        else:
            xff = "—"
        origin = _short(hdrs.get("origin", ""), 80)
        ua = _short(hdrs.get("user-agent", ""), 160)
        mcp_sess = hdrs.get("mcp-session-id", "")
        if mcp_sess:
            mcp_sess = _short(mcp_sess, 20)
        else:
            mcp_sess = "—"
        mcp_ver = _short(hdrs.get("mcp-protocol-version", ""), 24)

        status_out: list[int | None] = [None]

        async def send_wrapper(message: dict) -> None:
            if message["type"] == "http.response.start":
                status_out[0] = message.get("status")
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            st = status_out[0] if status_out[0] is not None else "—"
            log.info(
                "mcp_client peer=%s x_forwarded_for=%s session=%s mcp_protocol=%s origin=%s ua=%s %s %s -> %s",
                peer,
                xff,
                mcp_sess,
                mcp_ver,
                origin,
                ua,
                scope.get("method", "?"),
                scope.get("path", "?"),
                st,
            )
