from __future__ import annotations

import asyncio
import email.utils
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

import feedparser
import httpx

from mcp_news_server.models import NewsItem

log = logging.getLogger(__name__)

_HTML_TITLE_RE = re.compile(r"<title[^>]*>([^<]{1,500})</title>", re.I)


def _iso_from_struct(t: Any) -> str | None:
    if not t:
        return None
    try:
        dt = datetime(
            int(t.tm_year),
            int(t.tm_mon),
            int(t.tm_mday),
            int(t.tm_hour),
            int(t.tm_min),
            int(t.tm_sec),
            tzinfo=timezone.utc,
        )
        return dt.isoformat()
    except (TypeError, ValueError, AttributeError):
        return None


def _iso_from_rfc822(s: str | None) -> str | None:
    if not s:
        return None
    try:
        t = email.utils.parsedate_to_datetime(s.strip())
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return t.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


async def fetch_rss_via_http(
    client: httpx.AsyncClient,
    feed_url: str,
    *,
    feed_label: str,
    max_items: int,
) -> list[NewsItem]:
    """Fetch feed XML then parse (works better with custom headers and redirects)."""

    def _load_and_parse(content: bytes) -> list[NewsItem]:
        parsed = feedparser.parse(content)
        if getattr(parsed, "bozo", False) and not getattr(parsed, "entries", None):
            log.warning("RSS parse issue for %s: %s", feed_url, getattr(parsed, "bozo_exception", ""))
        title_fn = getattr(parsed.feed, "title", "") or feed_label or feed_url
        out: list[NewsItem] = []
        for ent in (parsed.entries or [])[:max_items]:
            link = (getattr(ent, "link", None) or getattr(ent, "id", "") or "").strip()
            if not link:
                continue
            raw_title = (getattr(ent, "title", None) or "").strip() or link
            summary = (getattr(ent, "summary", None) or getattr(ent, "description", None) or "").strip() or None
            published = None
            if getattr(ent, "published_parsed", None):
                published = _iso_from_struct(ent.published_parsed)
            if not published and getattr(ent, "updated_parsed", None):
                published = _iso_from_struct(ent.updated_parsed)
            if not published and getattr(ent, "published", None):
                published = _iso_from_rfc822(str(ent.published))
            out.append(
                NewsItem(
                    title=raw_title,
                    url=link,
                    summary=summary,
                    published=published,
                    source_type="rss",
                    source_name=str(title_fn),
                )
            )
        return out

    r = await client.get(feed_url, follow_redirects=True)
    r.raise_for_status()
    return await asyncio.to_thread(_load_and_parse, r.content)


def _meta_content(html: str, prop: str) -> str | None:
    pat = re.compile(
        rf'<meta\s+[^>]*(?:property|name)\s*=\s*["\']{re.escape(prop)}["\'][^>]*'
        rf'content\s*=\s*["\']([^"\'<]+)["\']',
        re.I,
    )
    m = pat.search(html[:500_000])
    if m:
        return m.group(1).strip()
    pat2 = re.compile(
        rf'<meta\s+[^>]*content\s*=\s*["\']([^"\'<]+)["\'][^>]*'
        rf'(?:property|name)\s*=\s*["\']{re.escape(prop)}["\']',
        re.I,
    )
    m2 = pat2.search(html[:500_000])
    return m2.group(1).strip() if m2 else None


def _extract_og(html: str) -> tuple[str | None, str | None]:
    title = _meta_content(html, "og:title") or _meta_content(html, "twitter:title")
    desc = _meta_content(html, "og:description") or _meta_content(html, "description")
    if not title:
        tm = _HTML_TITLE_RE.search(html[:200_000])
        if tm:
            title = re.sub(r"\s+", " ", tm.group(1)).strip()
    return title, desc


async def fetch_page_metadata(client: httpx.AsyncClient, page_url: str) -> NewsItem:
    r = await client.get(page_url, follow_redirects=True)
    r.raise_for_status()
    html = r.text
    t, d = _extract_og(html)
    title = t or page_url
    return NewsItem(
        title=title,
        url=str(r.url),
        summary=d,
        published=None,
        source_type="web",
        source_name=urlparse_host(str(r.url)),
    )


def urlparse_host(url: str) -> str:
    from urllib.parse import urlparse

    try:
        return (urlparse(url).hostname or "").lower() or "web"
    except Exception:
        return "web"


async def searx_search(
    client: httpx.AsyncClient,
    base_url: str,
    query: str,
    *,
    limit: int,
    categories: str | None = None,
) -> list[NewsItem]:
    """SearXNG JSON API: GET {base}/search?q=...&format=json"""
    root = base_url.rstrip("/")
    params: dict[str, str] = {"q": query, "format": "json"}
    if categories:
        params["categories"] = categories
    r = await client.get(f"{root}/search", params=params, follow_redirects=True)
    r.raise_for_status()
    data = r.json()
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        return []
    out: list[NewsItem] = []
    for row in results[:limit]:
        if not isinstance(row, dict):
            continue
        url = str(row.get("url") or "").strip()
        title = str(row.get("title") or "").strip() or url
        if not url:
            continue
        content = row.get("content")
        summary = str(content).strip() if content else None
        pub = row.get("publishedDate") or row.get("pubdate")
        published = str(pub).strip() if pub else None
        engine = row.get("engine")
        extra = {}
        if isinstance(engine, str):
            extra["engine"] = engine
        elif isinstance(engine, list):
            extra["engines"] = engine
        out.append(
            NewsItem(
                title=title,
                url=url,
                summary=summary,
                published=published,
                source_type="searx",
                source_name="searx",
                extra=extra,
            )
        )
    return out


def safe_json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)

