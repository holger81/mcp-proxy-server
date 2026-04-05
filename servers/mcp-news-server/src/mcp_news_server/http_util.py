from __future__ import annotations

import os

import httpx

_DEFAULT_UA = (
    "mcp-news-server/0.1 (+https://github.com/modelcontextprotocol; news curation MCP)"
)


def http_timeout_s() -> float:
    raw = os.environ.get("NEWS_MCP_HTTP_TIMEOUT", "").strip()
    if not raw:
        return 25.0
    try:
        return max(5.0, min(120.0, float(raw)))
    except ValueError:
        return 25.0


def async_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=http_timeout_s(),
        headers={"User-Agent": _DEFAULT_UA},
        follow_redirects=True,
    )
