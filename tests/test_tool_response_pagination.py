"""Tests for callTool response pagination."""

from __future__ import annotations

import json

from mcp import types as mcp_types

from mcp_proxy.settings import Settings
from mcp_proxy.tool_response_cache import ToolResponseCache
from mcp_proxy.tool_response_pagination import (
    paginate_call_tool_response,
    paginate_from_cache,
    parse_call_tool_pagination,
    parse_response_pagination,
    peel_pagination_params,
    wrap_hot_tool_as_call_tool,
)


def test_small_response_unchanged() -> None:
    settings = Settings(call_tool_response_page_chars=5000)
    cache = ToolResponseCache()
    blocks = [mcp_types.TextContent(type="text", text="hello")]
    out = paginate_call_tool_response(
        blocks,
        settings=settings,
        cache=cache,
        tool_name="srv__tool",
        pagination=parse_response_pagination({}, settings),
    )
    assert out == blocks


def test_peel_pagination_from_nested_arguments() -> None:
    settings = Settings(call_tool_response_page_chars=100)
    wrapped = wrap_hot_tool_as_call_tool(
        "mcp_news__news_today",
        {
            "force_refresh": False,
            "responseCacheId": "abc",
            "responseOffset": 100,
        },
    )
    assert wrapped["toolName"] == "mcp_news__news_today"
    assert wrapped["arguments"] == {"force_refresh": False}
    assert wrapped["responseCacheId"] == "abc"
    assert wrapped["responseOffset"] == 100

    clean, pag = peel_pagination_params(
        {"force_refresh": True, "responseCacheId": "x", "responseLimit": 50}
    )
    assert clean == {"force_refresh": True}
    assert pag["responseCacheId"] == "x"
    assert pag["responseLimit"] == 50

    upstream, pagination = parse_call_tool_pagination(
        {
            "toolName": "mcp_news__news_today",
            "arguments": {"force_refresh": False, "responseOffset": 5},
            "responseCacheId": "top-wins",
        },
        settings,
    )
    assert upstream == {"force_refresh": False}
    assert pagination.cache_id == "top-wins"
    assert pagination.offset == 5


def test_large_response_paginated() -> None:
    settings = Settings(call_tool_response_page_chars=100)
    cache = ToolResponseCache()
    text = "x" * 250
    blocks = [mcp_types.TextContent(type="text", text=text)]
    out = paginate_call_tool_response(
        blocks,
        settings=settings,
        cache=cache,
        tool_name="srv__tool",
        pagination=parse_response_pagination({}, settings),
    )
    assert len(out) == 1
    payload = json.loads(out[0].text)  # type: ignore[union-attr]
    assert payload["text"] == text[:100]
    assert payload["pagination"]["hasMore"] is True
    cache_id = payload["pagination"]["responseCacheId"]

    page2 = paginate_from_cache(
        cache.get(cache_id),  # type: ignore[arg-type]
        settings=settings,
        cache_id=cache_id,
        offset=100,
        limit=None,
        tool_name="srv__tool",
    )
    payload2 = json.loads(page2[0].text)  # type: ignore[union-attr]
    assert payload2["text"] == text[100:200]
    assert payload2["pagination"]["offset"] == 100
