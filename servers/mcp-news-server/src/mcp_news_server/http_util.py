from __future__ import annotations

import asyncio
import json
import os
from urllib.parse import urljoin

import httpx

from mcp_news_server.url_guard import UrlBlockedError, assert_safe_public_url

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


def feed_deadline_s() -> float:
    """Overall wall-clock deadline for fetching one feed.

    httpx's timeouts are per-operation inactivity gaps, so a connection that
    trickles a few bytes per second never trips them and can hold a whole
    digest refresh (and its lock) open indefinitely.
    """
    raw = os.environ.get("NEWS_MCP_FEED_DEADLINE_S", "").strip()
    if not raw:
        return 90.0
    try:
        return max(5.0, min(600.0, float(raw)))
    except ValueError:
        return 90.0


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
      URL may not redirect us into an internal one;
    - retries once with ``Accept-Encoding: identity`` when a body arrives
      tagged as compressed but is undecodable (transient CDN/network corruption
      observed as bursts of ``DecodingError``; the uncompressed retry rides
      past those windows).

    If the identity retry also fails, the error propagates unchanged.
    """
    try:
        return await _limited_get_once(client, url, params=params, max_bytes=max_bytes)
    except httpx.DecodingError:
        return await _limited_get_once(
            client,
            url,
            params=params,
            max_bytes=max_bytes,
            headers={"Accept-Encoding": "identity"},
        )


async def _limited_get_once(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, str] | None = None,
    max_bytes: int = MAX_RESPONSE_BYTES,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    current_url = url
    current_params = params
    for hop in range(MAX_REDIRECTS + 1):
        async with client.stream(
            "GET",
            current_url,
            params=current_params,
            headers=headers,
            follow_redirects=False,
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
            # ``aiter_bytes()`` already decompresses Content-Encoding (gzip,
            # brotli, …). Rebuilding a Response with the original encoding
            # headers makes httpx try to decode again → DecodingError
            # ("incorrect header check") on virtually every real RSS feed.
            # (325a7cf) Strip those headers here; keep the name distinct from
            # the per-request ``headers`` parameter used on retry hops.
            response_headers = httpx.Headers(
                [
                    (k, v)
                    for k, v in response.headers.multi_items()
                    if k.lower()
                    not in ("content-encoding", "content-length", "transfer-encoding")
                ]
            )
            extensions = response.extensions
        # Body fully read: leave the redirect loop. Without this break the
        # loop silently re-issued the request MAX_REDIRECTS+1 times per
        # successful GET (production: 21x traffic per feed refresh, tripping
        # CDN rate limits and corrupting bodies).
        break

    full = httpx.Response(
        status_code=status,
        headers=response_headers,
        content=content,
        request=request,
        extensions=extensions,
    )
    full.raise_for_status()
    return full


def classify_error(e: BaseException) -> str:
    """Short, stable error class for in-band tool responses (PLAN 3.3).

    Raw exception strings often embed internal URLs, ports or stack hints;
    tool responses only get the class, full details go to server logs.
    """
    if isinstance(e, ResponseTooLargeError):
        return "response_too_large"
    if isinstance(e, UrlBlockedError):
        return "url_blocked"
    if isinstance(e, (httpx.TimeoutException, asyncio.TimeoutError)):
        return "timeout"
    if isinstance(e, httpx.TooManyRedirects):
        return "too_many_redirects"
    if isinstance(e, httpx.HTTPStatusError):
        return f"http_{e.response.status_code}"
    if isinstance(e, httpx.DecodingError):
        return "decoding_error"
    if isinstance(e, httpx.InvalidURL):
        return "invalid_url"
    if isinstance(e, httpx.ConnectError):
        return "connect_failed"
    if isinstance(e, httpx.HTTPError):
        return "http_error"
    if isinstance(e, json.JSONDecodeError):
        return "invalid_json"
    return type(e).__name__.lower()
