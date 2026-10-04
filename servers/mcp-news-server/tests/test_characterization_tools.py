"""Characterization tests for the news server's tool handlers (PLAN.md PR 0.3).

Drives the registered CallToolRequest handler directly. Pins today's
contracts: validation errors surface as isError=True results, cached-digest
tools read the on-disk snapshot, and force_refresh rebuilds through the RSS
pipeline (curator skipped when LLM env is unset).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
import respx
from mcp import types as mt

from mcp_news_server.server import build_news_server

_CLEAN_ENV = (
    "SEARXNG_BASE_URL",
    "NEWS_MCP_LLM_MODEL",
    "NEWS_MCP_LLM_API_KEY",
    "NEWS_MCP_LLM_BASE_URL",
    "NEWS_MCP_LOCAL_SEARX_QUERIES",
    "NEWS_MCP_LOCAL_SEARX_CATEGORIES",
    "NEWS_MCP_CACHE_REFRESH_SECONDS",
    "NEWS_MCP_CACHE_MAX_PER_SOURCE",
    "NEWS_MCP_CACHE_MAX_TOTAL",
)

WIRE_RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>Wire</title>
<item><title>Alpha happens</title><link>https://wire.test/a</link><pubDate>Mon, 01 Sep 2025 09:00:00 GMT</pubDate></item>
<item><title>Beta follows</title><link>https://wire.test/b</link></item>
</channel></rss>"""


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
        return {
            "isError": bool(result.isError),
            "text": text,
            "payload": json.loads(text) if text.lstrip().startswith(("{", "[")) else None,
        }

    return _call


def run(coro):
    return asyncio.run(coro)


# --- feed management tools -------------------------------------------------


def test_feed_list_add_remove_roundtrip(call, tmp_path: Path):
    r = run(call("news_list_feeds", {}))
    assert not r["isError"]
    assert r["payload"]["dataDir"] == str(tmp_path)

    r = run(call("news_add_rss_feed", {"url": "https://a.test/rss", "label": "A"}))
    assert not r["isError"]
    assert any(f["url"] == "https://a.test/rss" for f in r["payload"]["feeds"])

    r = run(call("news_remove_rss_feed", {"url": "https://a.test/rss"}))
    assert not r["isError"]
    assert all(f["url"] != "https://a.test/rss" for f in r["payload"]["feeds"])


# --- validation contracts (isError results, not exceptions) ----------------


@pytest.mark.parametrize(
    ("tool", "args", "message"),
    [
        ("news_curate", {"digest_scope": "mars"}, "not one of"),
        ("news_curate", {"extra_urls": [f"https://e.test/{i}" for i in range(41)]}, "At most 40"),
        ("news_briefing", {"scope": "mars"}, "not one of"),
        ("news_briefing", {"digest_scope": "full"}, "Additional properties"),
        ("news_searx_search", {"query": "x"}, "SEARXNG_BASE_URL"),
        ("news_add_rss_feed", {}, "required"),
        ("totally_unknown_tool", {}, "Unknown tool"),
    ],
)
def test_validation_errors_are_isError_results(call, tool, args, message):
    r = run(call(tool, args))
    assert r["isError"] is True
    assert message in r["text"]


# --- cached-digest reads and refresh ---------------------------------------


def test_digest_tools_return_empty_cache_snapshots_with_scope_meta(call):
    for tool, digest in [
        ("news_today", "today"),
        ("news_germany", "germany"),
        ("news_local", "local"),
    ]:
        r = run(call(tool, {}))
        assert not r["isError"]
        assert r["payload"]["meta"]["digest"] == digest
        assert r["payload"]["meta"]["source"] == "cache"
        assert r["payload"]["itemCount"] == 0


def test_news_curate_without_live_args_reads_cache(call):
    r = run(call("news_curate", {}))
    assert not r["isError"]
    assert r["payload"]["meta"]["source"] == "cache"


def test_briefing_without_llm_notes_missing_briefing(call):
    r = run(call("news_briefing", {}))
    assert not r["isError"]
    assert r["payload"]["scope"] == "global"
    assert "No LLM briefing in cache" in r["payload"]["note"]


def test_force_refresh_builds_digest_and_writes_disk(call, tmp_path: Path):
    # One working feed; the three auto-appended supplemental feeds fail (500)
    # and are reported without aborting the digest. LLM curator is unconfigured
    # in this env, so the refresh completes without it.
    feeds_yaml = tmp_path / "feeds.yaml"
    feeds_yaml.write_text(
        "feeds:\n- url: https://wire.test/rss.xml\n  label: Wire\n  enabled: true\n",
        encoding="utf-8",
    )
    with respx.mock:
        # respx matches in registration order: specific route first, catch-all last.
        respx.get("https://wire.test/rss.xml").mock(
            return_value=httpx.Response(200, content=WIRE_RSS.encode())
        )
        respx.route().mock(return_value=httpx.Response(500))
        r = run(call("news_today", {"force_refresh": True}))

    assert not r["isError"]
    payload = r["payload"]
    assert payload["itemCount"] == 2
    assert payload["meta"]["digest"] == "today"
    assert payload["meta"]["rssFeedsUsed"] == 4  # wire + 3 supplementals
    assert len(payload["errors"]) == 3
    assert (tmp_path / "cache" / "today.json").is_file()


def test_news_curate_live_fetch_reports_rss_failures(call, tmp_path: Path):
    feeds_yaml = tmp_path / "feeds.yaml"
    feeds_yaml.write_text(
        "feeds:\n- url: https://wire.test/rss.xml\n  label: Wire\n  enabled: true\n",
        encoding="utf-8",
    )
    with respx.mock:
        respx.get("https://wire.test/rss.xml").mock(
            return_value=httpx.Response(200, content=WIRE_RSS.encode())
        )
        respx.route().mock(return_value=httpx.Response(500))
        r = run(call("news_curate", {"live_fetch": True}))

    assert not r["isError"]
    payload = r["payload"]
    assert payload["meta"]["liveFetch"] is True
    assert payload["meta"]["digestScope"] == "global"
    assert payload["itemCount"] == 2
