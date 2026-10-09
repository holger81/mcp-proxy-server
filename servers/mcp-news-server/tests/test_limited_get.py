"""PR 2.4 (PLAN.md Phase 2): response bodies are size-capped.

On main, fetchers buffered entire responses with no limit, so a hostile or
misconfigured endpoint could exhaust memory. `limited_get` aborts on a large
Content-Length header before reading, and mid-stream when the accumulated
body exceeds the cap.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from mcp_news_server.fetchers import fetch_rss_via_http, searx_search
from mcp_news_server.http_util import (
    MAX_RESPONSE_BYTES,
    ResponseTooLargeError,
    limited_get,
)

WIRE_RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>Wire</title>
<item><title>Alpha happens</title><link>https://wire.test/a</link></item>
</channel></rss>"""


@respx.mock
def test_small_body_returns_normally():
    respx.get("https://wire.test/rss.xml").mock(
        return_value=httpx.Response(200, content=WIRE_RSS.encode())
    )

    async def run():
        async with httpx.AsyncClient() as client:
            r = await limited_get(client, "https://wire.test/rss.xml")
        assert r.status_code == 200
        assert r.content == WIRE_RSS.encode()

    asyncio.run(run())


@respx.mock
def test_gzip_content_encoding_does_not_double_decode():
    """Real feeds send Content-Encoding: gzip; aiter_bytes already decompresses.

    Rebuilding the Response must drop encoding headers or httpx decompresses
    again and raises DecodingError (incorrect header check).
    """
    import gzip

    raw = WIRE_RSS.encode()
    compressed = gzip.compress(raw)
    respx.get("https://wire.test/gzip.xml").mock(
        return_value=httpx.Response(
            200,
            headers={
                "content-encoding": "gzip",
                "content-length": str(len(compressed)),
            },
            content=compressed,
        )
    )

    async def run():
        async with httpx.AsyncClient() as client:
            r = await limited_get(client, "https://wire.test/gzip.xml")
        assert r.status_code == 200
        assert r.content == raw
        assert "content-encoding" not in {k.lower() for k in r.headers.keys()}

    asyncio.run(run())


@respx.mock
def test_declared_content_length_rejected_before_download():
    respx.get("https://wire.test/big.xml").mock(
        return_value=httpx.Response(
            200, headers={"content-length": "999999999"}, content=b"tiny"
        )
    )

    async def run():
        async with httpx.AsyncClient() as client:
            with pytest.raises(ResponseTooLargeError, match="declares"):
                await limited_get(client, "https://wire.test/big.xml")

    asyncio.run(run())


@respx.mock
def test_streamed_body_over_limit_aborts():
    # Lying Content-Length: the pre-check passes, the accumulated-byte
    # abort has to catch the oversized body mid-stream.
    respx.get("https://wire.test/liar.xml").mock(
        return_value=httpx.Response(
            200, headers={"content-length": "10"}, content=b"x" * 5_000
        )
    )

    async def run():
        async with httpx.AsyncClient() as client:
            with pytest.raises(ResponseTooLargeError, match="exceeded"):
                await limited_get(
                    client, "https://wire.test/liar.xml", max_bytes=1_000
                )

    asyncio.run(run())


@respx.mock
def test_http_status_errors_still_raise():
    respx.get("https://wire.test/gone").mock(return_value=httpx.Response(404))

    async def run():
        async with httpx.AsyncClient() as client:
            with pytest.raises(httpx.HTTPStatusError):
                await limited_get(client, "https://wire.test/gone")

    asyncio.run(run())


@respx.mock
def test_rss_fetcher_rejects_oversized_feed():
    # 6 MB of junk passes for XML at the transport level; the cap must fire
    # before feedparser ever sees it (default limit is 5 MB).
    respx.get("https://wire.test/huge.xml").mock(
        return_value=httpx.Response(200, content=b"<rss>" + b" " * MAX_RESPONSE_BYTES)
    )

    async def run():
        async with httpx.AsyncClient() as client:
            with pytest.raises(ResponseTooLargeError):
                await fetch_rss_via_http(
                    client,
                    "https://wire.test/huge.xml",
                    feed_label="Huge",
                    max_items=10,
                )

    asyncio.run(run())


@respx.mock
def test_rss_and_searx_paths_unaffected_within_cap():
    respx.get("https://wire.test/rss.xml").mock(
        return_value=httpx.Response(200, content=WIRE_RSS.encode())
    )
    respx.get("https://searx.test/search").mock(
        return_value=httpx.Response(
            200,
            json={"results": [{"url": "https://a.test/1", "title": "One"}]},
        )
    )

    async def run():
        async with httpx.AsyncClient() as client:
            items = await fetch_rss_via_http(
                client, "https://wire.test/rss.xml", feed_label="Wire", max_items=10
            )
            assert [i.title for i in items] == ["Alpha happens"]
            rows = await searx_search(
                client, "https://searx.test", "q", limit=5
            )
            assert [r.url for r in rows] == ["https://a.test/1"]

    asyncio.run(run())
