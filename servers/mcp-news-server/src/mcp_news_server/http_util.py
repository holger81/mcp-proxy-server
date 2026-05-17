from __future__ import annotations

import os

import httpx

_DEFAULT_UA = (
    "Mozilla/5.0 (compatible; mcp-news-server/0.1; +https://github.com/modelcontextprotocol)"
)
_DEFAULT_HEADERS = {
    "User-Agent": _DEFAULT_UA,
    "Accept": "application/rss+xml, application/xml, text/xml, application/atom+xml, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


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
        headers=dict(_DEFAULT_HEADERS),
        follow_redirects=True,
    )
