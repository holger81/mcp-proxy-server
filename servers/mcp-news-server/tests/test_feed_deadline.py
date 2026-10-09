"""PR 42: each feed fetch gets an overall deadline.

httpx timeouts are per-operation inactivity gaps, so a connection trickling a
few bytes per second never trips them. Production saw a digest refresh cycle
stall ~90 minutes that way (single hung fetch holding the refresh lock).
``gather_rss_for_feeds`` now bounds every feed with ``asyncio.wait_for``.
"""

from __future__ import annotations

import asyncio

import httpx

from mcp_news_server.fetchers import gather_rss_for_feeds
from mcp_news_server.http_util import classify_error, feed_deadline_s
from mcp_news_server.models import FeedEntry, NewsItem


def test_deadline_env_bounds(monkeypatch):
    monkeypatch.delenv("NEWS_MCP_FEED_DEADLINE_S", raising=False)
    assert feed_deadline_s() == 90.0
    monkeypatch.setenv("NEWS_MCP_FEED_DEADLINE_S", "1")
    assert feed_deadline_s() == 5.0
    monkeypatch.setenv("NEWS_MCP_FEED_DEADLINE_S", "99999")
    assert feed_deadline_s() == 600.0
    monkeypatch.setenv("NEWS_MCP_FEED_DEADLINE_S", "nonsense")
    assert feed_deadline_s() == 90.0


def test_classify_maps_wait_for_timeout_to_timeout():
    assert classify_error(asyncio.TimeoutError()) == "timeout"


def test_hung_feed_times_out_without_blocking_others(monkeypatch):
    monkeypatch.setenv("NEWS_MCP_FEED_DEADLINE_S", "0.05")
    feeds = [
        FeedEntry(url="https://hang.test/rss.xml", label="Hang", enabled=True),
        FeedEntry(url="https://fast.test/rss.xml", label="Fast", enabled=True),
    ]
    rss = (
        b'<?xml version="1.0"?><rss version="2.0"><channel><title>F</title>'
        b"<item><title>Quick</title><link>https://fast.test/1</link></item>"
        b"</channel></rss>"
    )

    async def fake_http(client, url, **kwargs):
        if "hang" in url:
            await asyncio.sleep(30)
        return [
            NewsItem(
                title="Quick",
                url="https://fast.test/1",
                summary=None,
                published=None,
                source_type="rss",
                source_name="Fast",
            )
        ]

    monkeypatch.setattr(
        "mcp_news_server.fetchers.fetch_rss_via_http", fake_http
    )

    async def run():
        errors: list[dict[str, str]] = []
        async with httpx.AsyncClient() as client:
            items = await gather_rss_for_feeds(
                client, feeds, max_per=10, errors=errors
            )
        assert [i.title for i in items] == ["Quick"]
        assert errors == [{"source": "https://hang.test/rss.xml", "error": "timeout"}]

    asyncio.run(asyncio.wait_for(run(), timeout=10))
