"""PR 1.4 nits: removed-flag, bool rejection in _int, doc strings."""

from __future__ import annotations

import asyncio
import json

import pytest
from mcp import types as mt
from mcp.shared.exceptions import McpError

from mcp_news_server.server import _int, build_news_server, build_tool_list


@pytest.fixture
def call(tmp_path, monkeypatch):
    monkeypatch.setenv("NEWS_MCP_DATA_DIR", str(tmp_path))
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
        return json.loads(text)

    return _call


def test_remove_reports_removed_flag(call):
    run = asyncio.run
    run(call("news_add_rss_feed", {"url": "https://gone.test/rss"}))
    r = run(call("news_remove_rss_feed", {"url": "https://gone.test/rss"}))
    assert r["removed"] is True
    r2 = run(call("news_remove_rss_feed", {"url": "https://gone.test/rss"}))
    assert r2["removed"] is False


def test_int_rejects_booleans():
    with pytest.raises(McpError, match="not a boolean"):
        _int({"limit": True}, "limit", 15, min_v=1, max_v=50)
    # normal values still work
    assert _int({"limit": 5}, "limit", 15, min_v=1, max_v=50) == 5


def test_doc_strings_document_behavior():
    tools = {t.name: t for t in build_tool_list()}
    assert "`removed: true/false`" in tools["news_remove_rss_feed"].description
    assert "cached path returns the stored digest as-is" in tools["news_curate"].description
