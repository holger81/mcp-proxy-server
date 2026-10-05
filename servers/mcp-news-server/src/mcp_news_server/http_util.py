from __future__ import annotations

import os
from urllib.parse import urljoin

import httpx

from mcp_news_server.url_guard import assert_safe_public_url

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
MAX_REDIRECTS = 20  # same default as httpx


class ResponseTooLargeError(httpx.HTTPError):
    """A response body exceeded the configured size limit."""


async def limited_get(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, str] | None = None,
    max_bytes: int = MAX_RESPONSE_BYTES,
) -> httpx.Response:
    """GET with a hard body cap and SSRF-guarded redirects.

    Mirrors ``client.get(url, follow_redirects=True)`` followed by
    ``raise_for_status()``, but:

    - aborts before buffering an unbounded body — up-front on a too-large
      ``Content-Length``, mid-stream on accumulated bytes (missing/lying header);
    - follows redirects manually (httpx 0.28 has no redirect event hook) so
      every hop is re-validated by :func:`assert_safe_public_url`; a public
      URL may not redirect us into an internal one.
    """
    current_url = url
    current_params = params
    for hop in range(MAX_REDIRECTS + 1):
        async with client.stream(
            "GET", current_url, params=current_params, follow_redirects=False
        ) as response:
            if response.has_redirect_location and hop < MAX_REDIRECTS:
                next_url = urljoin(
                    str(response.request.url), response.headers["location"]
                )
                assert_safe_public_url(next_url)
                current_url, current_params = next_url, None
                continue
            if response.has_redirect_location:
                raise httpx.TooManyRedirects(
                    f"exceeded {MAX_REDIRECTS} redirects",
                    request=response.request,
                )
            declared = response.headers.get("content-length", "")
            if declared.isdecimal() and int(declared) > max_bytes:
                raise ResponseTooLargeError(
                    f"response for {current_url} declares {declared} bytes "
                    f"(limit {max_bytes})"
                )
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise ResponseTooLargeError(
                        f"response for {current_url} exceeded {max_bytes} "
                        "bytes while streaming"
                    )
            content = bytes(body)
            request = response.request
            status = response.status_code
            headers = response.headers
            extensions = response.extensions

    full = httpx.Response(
        status_code=status,
        headers=headers,
        content=content,
        request=request,
        extensions=extensions,
    )
    full.raise_for_status()
    return full
