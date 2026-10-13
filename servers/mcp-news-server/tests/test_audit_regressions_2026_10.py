"""Regression tests for the news-server half of AUDIT-2026-10-FINDINGS.md.

- SEC-4: every fetch re-validates its *first hop* (add-time validation is
  stale: DNS-rebind TOCTOU or hand-edited feeds.yaml).
- SEC-5 / L-10 / L-11: in-band error strings stay generic — internal URLs,
  exception reprs and guard details must not reach tool results.
- L-8: ``news_curate`` caps ``searx_queries`` like ``extra_urls`` already was.
- L-9: invalid URL ports surface as ``UrlBlockedError``, not ``ValueError``.
- L-13: the LLM sanitizer strips Unicode bidi isolates (Trojan Source).
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx
from mcp import types as mt

from mcp_news_server import url_guard
from mcp_news_server.fetchers import fetch_page_metadata, fetch_rss_via_http
from mcp_news_server.http_util import async_client
from mcp_news_server.llm_curator import _sanitize
from mcp_news_server.server import build_news_server
from mcp_news_server.url_guard import UrlBlockedError, assert_safe_public_url

RSS = (
    b'<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>'
    b"<item><title>Hi</title><link>https://ok.test/1</link></item>"
    b"</channel></rss>"
)


# -- L-9: invalid port is a blocked URL, not a crash ---------------------------


def test_invalid_port_raises_url_blocked() -> None:
    with pytest.raises(UrlBlockedError, match="invalid port"):
        assert_safe_public_url("http://example.test:notaport/rss")


def test_out_of_range_port_raises_url_blocked() -> None:
    with pytest.raises(UrlBlockedError, match="invalid port"):
        assert_safe_public_url("http://example.test:70000/rss")


# -- SEC-4: first hop re-validated on every fetch -------------------------------


@respx.mock
def test_fetch_rss_blocks_internal_literal_even_with_route_registered() -> None:
    # The transport route exists to prove the guard runs *before* any HTTP:
    # pre-fix, a stored http://127.0.0.1/... feed was fetched unguarded.
    route = respx.get("http://127.0.0.1/feed.xml").mock(
        return_value=httpx.Response(200, content=RSS)
    )

    async def run() -> None:
        async with async_client() as client:
            with pytest.raises(UrlBlockedError, match="internal address"):
                await fetch_rss_via_http(
                    client, "http://127.0.0.1/feed.xml", feed_label="x", max_items=5
                )

    asyncio.run(run())
    assert not route.called


@respx.mock
def test_fetch_page_metadata_blocks_link_local_metadata() -> None:
    route = respx.get("http://169.254.169.254/latest/meta-data/").mock(
        return_value=httpx.Response(200, text="secret")
    )

    async def run() -> None:
        async with async_client() as client:
            with pytest.raises(UrlBlockedError):
                await fetch_page_metadata(
                    client, "http://169.254.169.254/latest/meta-data/"
                )

    asyncio.run(run())
    assert not route.called


@respx.mock
def test_fetch_rss_blocks_when_dns_rebinds_to_internal(monkeypatch) -> None:
    # Add-time said public; now the name answers 10.x — the fetch must notice.
    monkeypatch.setattr(
        url_guard.socket,
        "getaddrinfo",
        lambda host, port, *a, **k: [(2, 1, 6, "", ("10.0.0.5", 80))],
    )

    async def run() -> None:
        async with async_client() as client:
            with pytest.raises(UrlBlockedError, match="internal address"):
                await fetch_rss_via_http(
                    client, "https://rebind.test/rss", feed_label="x", max_items=5
                )

    asyncio.run(run())


# -- L-13: bidi isolates stripped ------------------------------------------------


def test_sanitizer_strips_bidi_isolates() -> None:
    payload = "title \u2066eval\u2069 execute \u202e cmd"
    out = _sanitize(payload)
    for ch in ("\u2066", "\u2067", "\u2068", "\u2069", "\u202e"):
        assert ch not in out
    assert "title" in out and "execute" in out


# -- handler-level contracts ------------------------------------------------------

_CLEAN_ENV = (
    "SEARXNG_BASE_URL",
    "NEWS_MCP_LLM_MODEL",
    "NEWS_MCP_LLM_API_KEY",
    "NEWS_MCP_LLM_BASE_URL",
    "NEWS_MCP_LOCAL_SEARX_QUERIES",
    "NEWS_MCP_LOCAL_SEARX_CATEGORIES",
    "NEWS_MCP_ALLOW_URLS",
)


@pytest.fixture
def call(tmp_path, monkeypatch):
    monkeypatch.setenv("NEWS_MCP_DATA_DIR", str(tmp_path))
    for key in _CLEAN_ENV:
        monkeypatch.delenv(key, raising=False)
    server = build_news_server()
    handler = server.request_handlers[mt.CallToolRequest]

    async def _call(name: str, args: dict) -> dict:
        req = mt.CallToolRequest(
            method="tools/call",
            params=mt.CallToolRequestParams(name=name, arguments=args),
        )
        res = await handler(req)
        result = res.root if hasattr(res, "root") else res
        text = "".join(b.text for b in result.content if getattr(b, "type", "") == "text")
        return {"isError": bool(result.isError), "text": text}

    return _call


def run(coro):
    return asyncio.run(coro)


def test_curate_caps_searx_queries(call) -> None:
    r = run(call("news_curate", {"searx_queries": [f"q{i}" for i in range(41)]}))
    assert r["isError"] is True
    assert "At most 40 searx_queries" in r["text"]


def test_add_feed_blocked_message_is_generic(call) -> None:
    # L-11: classification stays in the log; the tool result must not reveal
    # *why* the URL was rejected (internal address vs blocked TLD, etc).
    r = run(call("news_add_rss_feed", {"url": "http://10.9.9.9/rss"}))
    assert r["isError"] is True
    assert "URL rejected by safety guard." in r["text"]
    assert "internal" not in r["text"].lower()
    assert "10.9.9.9" not in r["text"]


@respx.mock
def test_searx_search_failure_is_classified_not_leaked(call, monkeypatch) -> None:
    # L-10/SEC-5: SearXNG answering HTML used to raise json.JSONDecodeError
    # whose repr (with the internal URL) landed in-band. Now: fixed labels.
    monkeypatch.setenv("SEARXNG_BASE_URL", "http://sx.internal.test")
    respx.get("http://sx.internal.test/search").mock(
        return_value=httpx.Response(200, text="<html>502 upstream</html>")
    )
    r = run(call("news_searx_search", {"query": "anything"}))
    assert r["isError"] is True
    assert "SearXNG search failed: invalid_json." in r["text"]
    assert "sx.internal.test" not in r["text"]
    assert "JSONDecodeError" not in r["text"]
