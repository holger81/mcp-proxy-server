"""PR 5.5: param namespacing, collision-safe peeling, cache-name validation, mixed-content caps."""

from __future__ import annotations

import pytest
from mcp import types as mcp_types
from mcp.shared.exceptions import McpError

from mcp_proxy.settings import Settings
from mcp_proxy.tool_response_cache import ToolResponseCache
from mcp_proxy.tool_response_pagination import (
    paginate_call_tool_response,
    paginate_from_cache,
    parse_call_tool_pagination,
    peel_pagination_params,
    wrap_hot_tool_as_call_tool,
)


def _settings(page: int = 100) -> Settings:
    return Settings(call_tool_response_page_chars=page)


# -- namespaced + protected peeling ---------------------------------------------


def test_namespaced_keys_always_stripped() -> None:
    clean, pag = peel_pagination_params(
        {"q": 1, "_proxy.responseOffset": 10, "_proxy.responseLimit": 50}
    )
    assert clean == {"q": 1}
    assert pag == {"responseOffset": 10, "responseLimit": 50}


def test_protected_legacy_names_reach_upstream() -> None:
    raw = {"responseOffset": 42, "responseLimit": 7, "q": "x"}
    clean, pag = peel_pagination_params(raw, protected=frozenset({"responseOffset"}))
    assert clean == {"responseOffset": 42, "q": "x"}  # upstream's own param kept
    assert pag == {"responseLimit": 7}  # non-declared legacy still proxy-owned


def test_namespaced_wins_over_legacy_duplicate() -> None:
    clean, pag = peel_pagination_params(
        {"responseOffset": 1, "_proxy.responseOffset": 2}
    )
    assert clean == {}
    assert pag == {"responseOffset": 2}


def test_wrap_hot_tool_declared_params() -> None:
    out = wrap_hot_tool_as_call_tool(
        "srv__tool",
        {"responseOffset": 9, "responseLimit": 3},
        declared_params=frozenset({"responseOffset"}),
    )
    assert out == {
        "toolName": "srv__tool",
        "arguments": {"responseOffset": 9},
        "responseLimit": 3,
    }


def test_parse_precedence_top_namespaced_over_nested_legacy() -> None:
    settings = _settings()
    args = {
        "toolName": "srv__tool",
        "arguments": {"responseOffset": 5, "keep": 1},
        "_proxy.responseOffset": 20,
    }
    clean, req = parse_call_tool_pagination(args, settings)
    assert clean == {"keep": 1}
    assert req.offset == 20


def test_legacy_nested_still_peeled_without_declaration() -> None:
    settings = _settings()
    clean, req = parse_call_tool_pagination(
        {"toolName": "srv__tool", "arguments": {"responseOffset": 5}}, settings
    )
    assert clean == {}
    assert req.offset == 5


# -- cache page validation across name spellings ---------------------------------


def test_cache_replay_accepts_legacy_spelling() -> None:
    settings = _settings()
    cache = ToolResponseCache()
    text = "x" * 250
    cache_id = cache.put("srv__tool", text)
    entry = cache.get(cache_id)
    assert entry is not None
    # Replaying via legacy "srv/tool" used to be rejected by raw string compare.
    out = paginate_from_cache(
        entry,
        settings=settings,
        cache_id=cache_id,
        offset=0,
        limit=None,
        tool_name="srv/tool",
    )
    assert "x" * 10 in out[0].text
    # Hex fallback spelling too.
    out2 = paginate_from_cache(
        entry,
        settings=settings,
        cache_id=cache_id,
        offset=0,
        limit=None,
        tool_name="srv__p__" + "tool".encode().hex(),
    )
    assert "x" * 10 in out2[0].text


def test_cache_replay_rejects_different_tool() -> None:
    settings = _settings()
    cache = ToolResponseCache()
    cache_id = cache.put("srv__tool", "y" * 250)
    entry = cache.get(cache_id)
    assert entry is not None
    with pytest.raises(McpError):
        paginate_from_cache(
            entry,
            settings=settings,
            cache_id=cache_id,
            offset=0,
            limit=None,
            tool_name="other__tool",
        )


# -- mixed content no longer bypasses limits --------------------------------------


def _image() -> mcp_types.ImageContent:
    return mcp_types.ImageContent(type="image", data="aGk=", mimeType="image/png")


def test_mixed_content_text_truncated_to_page() -> None:
    settings = _settings(page=100)
    big = "z" * 5000
    blocks = [mcp_types.TextContent(type="text", text=big), _image()]
    out = paginate_call_tool_response(
        blocks,
        settings=settings,
        cache=ToolResponseCache(),
        tool_name="srv__tool",
        pagination=parse_call_tool_pagination({"toolName": "srv__tool"}, settings)[1],
    )
    texts = [b for b in out if isinstance(b, mcp_types.TextContent)]
    images = [b for b in out if isinstance(b, mcp_types.ImageContent)]
    assert len(images) == 1  # media passes through untouched
    assert len(texts[0].text) <= 100 + 20  # capped with suffix, not 5000
    assert texts[0].text.endswith("…[truncated]")


def test_mixed_content_short_text_untouched() -> None:
    settings = _settings(page=100)
    blocks = [mcp_types.TextContent(type="text", text="short"), _image()]
    out = paginate_call_tool_response(
        blocks,
        settings=settings,
        cache=ToolResponseCache(),
        tool_name="srv__tool",
        pagination=parse_call_tool_pagination({"toolName": "srv__tool"}, settings)[1],
    )
    assert [b.text for b in out if isinstance(b, mcp_types.TextContent)] == ["short"]
