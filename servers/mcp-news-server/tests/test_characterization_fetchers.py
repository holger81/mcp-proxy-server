"""Characterization tests for fetchers parsing (PLAN.md PR 0.3).

Pins today's (correct) RSS/Atom/web/SearXNG parsing behavior with respx.
Deliberately NOT characterized: dedupe/sort ordering in digest payloads —
the sort polarity is a known bug fixed in Phase 1 with its own tests.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from mcp_news_server.fetchers import (
    fetch_page_metadata,
    fetch_rss_via_http,
    gather_rss_for_feeds,
    searx_search,
)
from mcp_news_server.models import FeedEntry

RSS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>Test Wire</title><link>https://wire.test/</link>
  <item>
    <title>First Story</title>
    <link>https://wire.test/first?utm_source=feed</link>
    <description>Alpha summary</description>
    <pubDate>Mon, 01 Sep 2025 12:00:00 GMT</pubDate>
  </item>
  <item>
    <title>Second Story</title>
    <link>https://wire.test/second</link>
    <pubDate>Tue, 02 Sep 2025 00:00:00 +0200</pubDate>
  </item>
  <item>
    <link>https://wire.test/third</link>
  </item>
  <item><title>Item without link</title></item>
</channel></rss>
"""

ATOM_XML = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom Wire</title>
  <entry>
    <title>Atom One</title>
    <link href="https://atom.test/one"/>
    <updated>2025-09-01T10:00:00Z</updated>
    <summary>Sum one</summary>
  </entry>
</feed>
"""


def run(coro):
    return asyncio.run(coro)


def test_fetch_rss_parses_items_dates_and_skips_linkless():
    async def go():
        import respx

        async with httpx.AsyncClient() as client:
            with respx.mock:
                respx.get("https://wire.test/rss.xml").mock(
                    return_value=httpx.Response(
                        200, content=RSS_XML.encode(), headers={"content-type": "application/rss+xml"}
                    )
                )
                return await fetch_rss_via_http(
                    client, "https://wire.test/rss.xml", feed_label="fallback", max_items=10
                )

    items = run(go())
    assert [i.title for i in items] == ["First Story", "Second Story", "https://wire.test/third"]
    first, second = items[0], items[1]
    assert first.url == "https://wire.test/first?utm_source=feed"
    assert first.summary == "Alpha summary"
    assert first.published == "2025-09-01T12:00:00+00:00"
    # +0200 timestamp normalized to UTC by feedparser's parsed structs.
    assert second.published == "2025-09-01T22:00:00+00:00"
    assert items[2].published is None
    assert all(i.source_type == "rss" for i in items)
    assert all(i.source_name == "Test Wire" for i in items)


def test_fetch_rss_respects_max_items():
    import respx

    async def go():
        async with httpx.AsyncClient() as client:
            with respx.mock:
                respx.get("https://wire.test/rss.xml").mock(
                    return_value=httpx.Response(200, content=RSS_XML.encode())
                )
                return await fetch_rss_via_http(
                    client, "https://wire.test/rss.xml", feed_label="f", max_items=1
                )

    assert len(run(go())) == 1


def test_fetch_atom_parses_entry():
    import respx

    async def go():
        async with httpx.AsyncClient() as client:
            with respx.mock:
                respx.get("https://atom.test/feed").mock(
                    return_value=httpx.Response(200, content=ATOM_XML.encode())
                )
                return await fetch_rss_via_http(
                    client, "https://atom.test/feed", feed_label="f", max_items=10
                )

    items = run(go())
    assert len(items) == 1
    it = items[0]
    assert it.title == "Atom One"
    assert it.url == "https://atom.test/one"
    assert it.summary == "Sum one"
    assert it.published == "2025-09-01T10:00:00+00:00"
    assert it.source_name == "Atom Wire"


def test_gather_collects_errors_without_aborting_feeds():
    import respx

    async def go():
        feeds = [
            FeedEntry(url="https://wire.test/rss.xml", label="ok"),
            FeedEntry(url="https://broken.test/rss.xml", label="broken"),
        ]
        errors: list[dict[str, str]] = []
        async with httpx.AsyncClient() as client:
            with respx.mock:
                respx.get("https://wire.test/rss.xml").mock(
                    return_value=httpx.Response(200, content=RSS_XML.encode())
                )
                respx.get("https://broken.test/rss.xml").mock(
                    return_value=httpx.Response(500)
                )
                items = await gather_rss_for_feeds(client, feeds, max_per=10, errors=errors)
        return items, errors

    items, errors = run(go())
    assert len(items) == 3
    assert len(errors) == 1
    assert errors[0]["source"] == "https://broken.test/rss.xml"


SEARX_RESULTS = {
    "results": [
        {
            "url": "https://story.test/a",
            "title": "Story A",
            "content": "Excerpt A",
            "publishedDate": "2025-09-01T08:00:00",
            "engine": ["google", "bing"],
        },
        {"url": "https://story.test/b", "title": "Story B"},
        {"title": "row without url is skipped"},
    ]
}


def test_searx_search_parses_and_limits():
    import respx

    async def go():
        async with httpx.AsyncClient() as client:
            with respx.mock:
                respx.get("https://sx.test/search").mock(
                    return_value=httpx.Response(200, json=SEARX_RESULTS)
                )
                return await searx_search(
                    client, "https://sx.test/", "kw", limit=2, categories="news"
                )

    items = run(go())
    assert [i.url for i in items] == ["https://story.test/a", "https://story.test/b"]
    a = items[0]
    assert a.summary == "Excerpt A"
    # PR 1.3 intentionally changed this: SearXNG dates normalize to ISO-UTC
    # (was the raw string "2025-09-01T08:00:00").
    assert a.published == "2025-09-01T08:00:00+00:00"
    assert a.source_type == "searx"
    assert a.extra == {"engines": ["google", "bing"]}
    assert items[1].summary is None


def test_searx_search_tolerates_missing_results_key():
    import respx

    async def go():
        async with httpx.AsyncClient() as client:
            with respx.mock:
                respx.get("https://sx.test/search").mock(
                    return_value=httpx.Response(200, json={"results": {"bad": True}})
                )
                return await searx_search(client, "https://sx.test", "kw", limit=5)

    assert run(go()) == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2025-09-01T08:00:00Z", "2025-09-01T08:00:00+00:00"),
        ("2025-09-01T08:00:00", "2025-09-01T08:00:00+00:00"),
        ("2025-09-01T10:30:00+02:30", "2025-09-01T08:00:00+00:00"),
        ("Mon, 01 Sep 2025 08:00:00 GMT", "2025-09-01T08:00:00+00:00"),
        ("not a date", None),
    ],
)
def test_searx_search_normalizes_published_dates(raw, expected):
    import respx

    payload = {"results": [{"url": "https://story.test/x", "title": "X", "publishedDate": raw}]}

    async def go():
        async with httpx.AsyncClient() as client:
            with respx.mock:
                respx.get("https://sx.test/search").mock(
                    return_value=httpx.Response(200, json=payload)
                )
                return await searx_search(client, "https://sx.test", "kw", limit=5)

    assert run(go())[0].published == expected


def test_fetch_page_metadata_prefers_op_graph():
    import respx

    html = (
        "<html><head>"
        '<meta property="og:title" content="OG Title"/>'
        '<meta name="description" content="Meta desc"/>'
        "<title>Fallback Title</title>"
        "</head><body>hi</body></html>"
    )

    async def go():
        async with httpx.AsyncClient() as client:
            with respx.mock:
                respx.get("https://page.test/article").mock(
                    return_value=httpx.Response(200, text=html)
                )
                return await fetch_page_metadata(client, "https://page.test/article")

    it = run(go())
    assert it.title == "OG Title"
    assert it.summary == "Meta desc"
    assert it.source_type == "web"
    assert it.source_name == "page.test"
    assert it.published is None


def test_fetch_page_metadata_falls_back_to_html_title():
    import respx

    html = "<html><head><title>Plain   Title  Here</title></head><body></body></html>"

    async def go():
        async with httpx.AsyncClient() as client:
            with respx.mock:
                respx.get("https://page.test/plain").mock(
                    return_value=httpx.Response(200, text=html)
                )
                return await fetch_page_metadata(client, "https://page.test/plain")

    it = run(go())
    assert it.title == "Plain Title Here"
    assert it.summary is None
