"""PR 5.2a (D4 warn-first): upstream isError/structuredContent propagation.

Default (flag off) keeps today's behavior: content blocks only, upstream
tool errors arrive as successful text. With
``MCP_PROXY_PROPAGATE_TOOL_ERRORS=true`` the proxy returns a full
``CallToolResult`` so the MCP SDK passes the semantics through verbatim.
"""

from __future__ import annotations

import logging

from mcp import types as mcp_types

from mcp_proxy.settings import Settings
from mcp_proxy.tool_response_pagination import apply_upstream_error_semantics

BLOCKS = [mcp_types.TextContent(type="text", text="boom")]


def _result(is_error: bool = False, structured: dict | None = None):
    return mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text", text="boom")],
        structuredContent=structured,
        isError=is_error,
    )


def test_flag_off_returns_plain_blocks() -> None:
    settings = Settings(propagate_tool_errors=False)
    out = apply_upstream_error_semantics(
        _result(is_error=True, structured={"a": 1}), list(BLOCKS), settings=settings
    )
    assert out == BLOCKS  # today's behavior, byte for byte


def test_flag_on_plain_result_stays_blocks() -> None:
    settings = Settings(propagate_tool_errors=True)
    out = apply_upstream_error_semantics(_result(), list(BLOCKS), settings=settings)
    assert out == BLOCKS  # no semantics to carry → SDK normalization path unchanged


def test_flag_on_error_result_keeps_isError() -> None:
    settings = Settings(propagate_tool_errors=True)
    out = apply_upstream_error_semantics(
        _result(is_error=True), list(BLOCKS), settings=settings
    )
    assert isinstance(out, mcp_types.CallToolResult)
    assert out.isError is True
    assert out.content == BLOCKS
    assert out.structuredContent is None


def test_flag_on_forwards_structured_content_untouched() -> None:
    settings = Settings(propagate_tool_errors=True)
    structured = {"items": [1, 2, 3], "nested": {"ok": True}}
    out = apply_upstream_error_semantics(
        _result(structured=structured), list(BLOCKS), settings=settings
    )
    assert isinstance(out, mcp_types.CallToolResult)
    assert out.isError is False
    assert out.structuredContent == structured


# -- startup warning -----------------------------------------------------------


def test_warns_when_unset(monkeypatch, caplog) -> None:
    monkeypatch.delenv("MCP_PROXY_PROPAGATE_TOOL_ERRORS", raising=False)
    settings = Settings()
    assert settings.propagate_tool_errors is False  # default unchanged (5.2a)
    with caplog.at_level(logging.WARNING, logger="mcp_proxy.settings"):
        settings.log_error_policy()
    msgs = [r.getMessage() for r in caplog.records]
    assert any("MCP_PROXY_PROPAGATE_TOOL_ERRORS" in m for m in msgs)


def test_silent_when_set(monkeypatch, caplog) -> None:
    monkeypatch.setenv("MCP_PROXY_PROPAGATE_TOOL_ERRORS", "true")
    settings = Settings()
    assert settings.propagate_tool_errors is True
    with caplog.at_level(logging.WARNING, logger="mcp_proxy.settings"):
        settings.log_error_policy()
    assert [r for r in caplog.records if "PROPAGATE" in r.getMessage()] == []
