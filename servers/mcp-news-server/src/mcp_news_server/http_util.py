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


MAX_RESPONSE_BYTES = 5 * 1024 * 1024  # real feeds are well under this


class ResponseTooLargeError(httpx.HTTPError):
    """A response body exceeded the configured size limit."""


async def limited_get(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, str] | None = None,
    max_bytes: int = MAX_RESPONSE_BYTES,
) -> httpx.Response:
    """GET with a hard cap on the body downloaded into memory.

    Mirrors ``client.get(url, follow_redirects=True)`` followed by
    ``raise_for_status()``, but aborts before buffering an unbounded body:
    immediately when ``Content-Length`` exceeds ``max_bytes``, or mid-stream
    when the accumulated bytes do (missing or lying header).
    """
    async with client.stream(
        "GET", url, params=params, follow_redirects=True
    ) as response:
        declared = response.headers.get("content-length", "")
        if declared.isdecimal() and int(declared) > max_bytes:
            raise ResponseTooLargeError(
                f"response for {url} declares {declared} bytes "
                f"(limit {max_bytes})"
            )
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > max_bytes:
                raise ResponseTooLargeError(
                    f"response for {url} exceeded {max_bytes} bytes "
                    "while streaming"
                )
        content = bytes(body)
        request = response.request

    full = httpx.Response(
        status_code=response.status_code,
        headers=response.headers,
        content=content,
        request=request,
        extensions=response.extensions,
    )
    full.raise_for_status()
    return full
