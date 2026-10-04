"""PR 1.2: newest-first sort ordering with undated items last.

On main, `_published_sort_key` used dated=(0,p)/undated=(1,"") with
reverse=True, putting UNDATED items first — the opposite of the docstring.
These tests fail on the old code and pass on the fix.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
import respx
from mcp import types as mt

from mcp_news_server.dedupe import dedupe_news_items
from mcp_news_server.digest_cache import DigestCache
from mcp_news_server.models import NewsItem
from mcp_news_server.server import build_news_server
from mcp_news_server.store import FeedStore


def item(title: str, url: str, published: str | None) -> NewsItem:
    return NewsItem(title=title, url=url, published=published, source_type="rss")


def test_dedupe_output_is_newest_first_undated_last():
    items = [
        item("B old", "https://x.test/1", "2025-09-01T00:00:00+00:00"),
        item("C undated", "https://x.test/2", None),
        item("A newest", "https://x.test/3", "2025-09-03T00:00:00+00:00"),
        item("D middle", "https://x.test/4", "2025-09-02T00:00:00+00:00"),
    ]
    out = dedupe_news_items(items, dedupe_urls=True, dedupe_titles=False)
    assert [i.title for i in out] == ["A newest", "D middle", "B old", "C undated"]


def test_dedupe_keeps_newer_duplicate_and_orders_correctly():
    items = [
        item("Undated story here for fingerprint length", "https://x.test/a", None),
        item("Dated story here for fingerprint length", "https://x.test/b", "2025-09-05T00:00:00+00:00"),
        item("Dated story here for fingerprint length", "https://x.test/b", "2025-09-01T00:00:00+00:00"),
    ]
    out = dedupe_news_items(items)
    assert [i.title for i in out] == [
        "Dated story here for fingerprint length",
        "Undated story here for fingerprint length",
    ]


# --- digest truncation keeps the newest items, not undated ones ------------

MIXED_RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>Mixed</title>
<item><title>Newest story</title><link>https://wire.test/3</link><pubDate>Wed, 03 Sep 2025 00:00:00 GMT</pubDate></item>
<item><title>Middle story</title><link>https://wire.test/2</link><pubDate>Tue, 02 Sep 2025 00:00:00 GMT</pubDate></item>
<item><title>No date story</title><link>https://wire.test/n</link></item>
<item><title>Oldest story</title><link>https://wire.test/1</link><pubDate>Mon, 01 Sep 2025 00:00:00 GMT</pubDate></item>
</channel></rss>"""


@pytest.fixture
def clean_env(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("NEWS_MCP_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("SEARXNG_BASE_URL", raising=False)
    for key in ("NEWS_MCP_LLM_MODEL", "NEWS_MCP_LLM_BASE_URL", "NEWS_MCP_LLM_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / "feeds.yaml").write_text(
        "feeds:\n- url: https://wire.test/rss.xml\n  label: Wire\n  enabled: true\n",
        encoding="utf-8",
    )
    return tmp_path


def _mock_rss() -> None:
    respx.get("https://wire.test/rss.xml").mock(
        return_value=httpx.Response(200, content=MIXED_RSS.encode())
    )
    respx.route().mock(return_value=httpx.Response(500))  # supplemental feeds


def test_digest_truncation_prefers_dated_newest(clean_env: Path, monkeypatch):
    monkeypatch.setenv("NEWS_MCP_CACHE_MAX_TOTAL", "2")
    with respx.mock:
        _mock_rss()
        cache = DigestCache(FeedStore(data_dir=clean_env))
        asyncio.run(cache.refresh_today())
        payload = cache.snapshot_today()
    assert [i["title"] for i in payload["items"]] == ["Newest story", "Middle story"]


def test_news_curate_without_dedupe_sorts_newest_first(clean_env: Path):
    server = build_news_server()
    handler = server.request_handlers[mt.CallToolRequest]

    async def go():
        req = mt.CallToolRequest(
            method="tools/call",
            params=mt.CallToolRequestParams(
                name="news_curate",
                arguments={"live_fetch": True, "deduplicate": False},
            ),
        )
        res = await handler(req)
        result = res.root if hasattr(res, "root") else res
        return json.loads(result.content[0].text)

    with respx.mock:
        _mock_rss()
        payload = asyncio.run(go())

    assert payload["meta"]["deduplicated"] is False
    titles = [i["title"] for i in payload["items"]]
    assert titles[:3] == ["Newest story", "Middle story", "Oldest story"]
    assert titles[-1] == "No date story"
