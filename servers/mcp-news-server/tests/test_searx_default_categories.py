"""Default SearXNG category is "news" (PLAN.md PR #43).

Unscoped SearXNG queries hit the *general* category, whose engines
(duckduckgo/brave/startpage/…) CAPTCHA and 429 home-IP instances — so
the news tools used to silently return zero items. Both call sites
(news_searx_search, news_curate) now default to the news category while
still honoring explicit overrides.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import respx
from mcp import types as mt

from mcp_news_server.server import build_news_server

SEARX_JSON = {
    "results": [
        {"title": "Headline one", "url": "https://ex.test/1", "content": "snippet one"},
    ]
}


@pytest.fixture
def call(tmp_path, monkeypatch):
    monkeypatch.setenv("NEWS_MCP_DATA_DIR", str(tmp_path))
    for key in (
        "SEARXNG_BASE_URL",
        "NEWS_MCP_LLM_MODEL",
        "NEWS_MCP_LLM_API_KEY",
        "NEWS_MCP_LLM_BASE_URL",
        "NEWS_MCP_LOCAL_SEARX_QUERIES",
        "NEWS_MCP_LOCAL_SEARX_CATEGORIES",
        "NEWS_MCP_CACHE_REFRESH_SECONDS",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("NEWS_MCP_ALLOW_URLS", "sx.test")
    monkeypatch.setenv("SEARXNG_BASE_URL", "https://sx.test")
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
        return {
            "isError": bool(result.isError),
            "text": text,
            "payload": json.loads(text) if text.lstrip().startswith(("{", "[")) else None,
        }

    return _call


def run(coro):
    return asyncio.run(coro)


@respx.mock
def test_searx_search_defaults_to_news_category(call):
    route = respx.get("https://sx.test/search").mock(
        return_value=httpx.Response(200, json=SEARX_JSON)
    )

    r = run(call("news_searx_search", {"query": "kw"}))

    assert not r["isError"], r["text"]
    assert route.calls
    assert route.calls[0].request.url.params.get("categories") == "news"
    assert r["payload"]["items"][0]["title"] == "Headline one"


@respx.mock
def test_searx_search_honors_explicit_categories(call):
    route = respx.get("https://sx.test/search").mock(
        return_value=httpx.Response(200, json=SEARX_JSON)
    )

    r = run(call("news_searx_search", {"query": "kw", "categories": "it"}))

    assert not r["isError"], r["text"]
    assert route.calls[0].request.url.params.get("categories") == "it"


@respx.mock
def test_curate_searx_defaults_to_news_category(call):
    route = respx.get("https://sx.test/search").mock(
        return_value=httpx.Response(200, json=SEARX_JSON)
    )

    r = run(call("news_curate", {"searx_queries": ["kw"]}))

    assert not r["isError"], r["text"]
    assert route.calls
    assert route.calls[0].request.url.params.get("categories") == "news"


@respx.mock
def test_curate_searx_honors_explicit_searx_categories(call):
    route = respx.get("https://sx.test/search").mock(
        return_value=httpx.Response(200, json=SEARX_JSON)
    )

    r = run(call("news_curate", {"searx_queries": ["kw"], "searx_categories": "general"}))

    assert not r["isError"], r["text"]
    assert route.calls[0].request.url.params.get("categories") == "general"
