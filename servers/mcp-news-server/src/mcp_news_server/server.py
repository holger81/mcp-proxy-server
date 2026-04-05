"""MCP server: news curation tools."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from mcp import types as mcp_types
from mcp.server import Server
from mcp.shared.exceptions import McpError

from mcp_news_server.dedupe import dedupe_news_items
from mcp_news_server.fetchers import fetch_page_metadata, fetch_rss_via_http, safe_json_dumps, searx_search
from mcp_news_server.http_util import async_client
from mcp_news_server.models import NewsItem
from mcp_news_server.store import FeedStore, default_data_dir

_INSTRUCTIONS = """\
This server curates world news from RSS feeds you configure, arbitrary article URLs (Open Graph / title), \
and SearXNG JSON search. Use `news_list_feeds` to see RSS sources, `news_add_rss_feed` / `news_remove_rss_feed` \
to curate them, `news_searx_search` for web search results, `news_ingest_urls` for one-off pages, and \
`news_curate` to pull everything together. Responses merge sources and deduplicate by canonical URL and \
(by default) by normalized headline to reduce syndicated duplicates. \
Optional env: `SEARXNG_BASE_URL` (e.g. https://search.example.com), `NEWS_MCP_DATA_DIR` (feed list storage), \
`NEWS_MCP_HTTP_TIMEOUT` (seconds, default 25).
"""


def _searx_base_from_env() -> str | None:
    v = os.environ.get("SEARXNG_BASE_URL", "").strip()
    return v or None


def _json_text(payload: Any) -> list[mcp_types.ContentBlock]:
    return [mcp_types.TextContent(type="text", text=json.dumps(payload, indent=2, ensure_ascii=False, default=str))]


def _err(message: str) -> McpError:
    return McpError(mcp_types.ErrorData(code=mcp_types.INVALID_PARAMS, message=message))


def _int(args: dict, key: str, default: int, *, min_v: int = 1, max_v: int = 500) -> int:
    if key not in args or args[key] is None:
        return default
    try:
        n = int(args[key])
    except (TypeError, ValueError):
        raise _err(f"{key!r} must be an integer.") from None
    if n < min_v or n > max_v:
        raise _err(f"{key!r} must be between {min_v} and {max_v}.")
    return n


def _bool(args: dict, key: str, default: bool) -> bool:
    if key not in args or args[key] is None:
        return default
    v = args[key]
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("1", "true", "yes", "on"):
            return True
        if s in ("0", "false", "no", "off", ""):
            return False
    raise _err(f"{key!r} must be a boolean.")


def _str_list(args: dict, key: str) -> list[str]:
    raw = args.get(key)
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise _err(f"{key!r} must be a list of strings.")
    out: list[str] = []
    for x in raw:
        if not isinstance(x, str) or not x.strip():
            raise _err(f"Each {key} entry must be a non-empty string.")
        out.append(x.strip())
    return out


def _optional_str(args: dict, key: str) -> str | None:
    v = args.get(key)
    if v is None:
        return None
    if not isinstance(v, str):
        raise _err(f"{key!r} must be a string.")
    s = v.strip()
    return s or None


def build_tool_list() -> list[mcp_types.Tool]:
    return [
        mcp_types.Tool(
            name="news_list_feeds",
            description="List configured RSS feed URLs (and labels) persisted under NEWS_MCP_DATA_DIR.",
            inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
        ),
        mcp_types.Tool(
            name="news_add_rss_feed",
            description="Add an RSS or Atom feed URL to the curated list (idempotent by normalized URL).",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Feed URL (https)."},
                    "label": {
                        "type": "string",
                        "description": "Optional human-readable name shown in results.",
                    },
                },
                "required": ["url"],
            },
        ),
        mcp_types.Tool(
            name="news_remove_rss_feed",
            description="Remove a feed by URL (matches normalized URL).",
            inputSchema={
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        ),
        mcp_types.Tool(
            name="news_searx_search",
            description=(
                "Query a SearXNG instance (JSON). Use `searx_base_url` or set env `SEARXNG_BASE_URL`. "
                "Returns title, url, snippet, optional published date."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query."},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 50,
                        "default": 15,
                        "description": "Max results to return.",
                    },
                    "categories": {
                        "type": "string",
                        "description": "Optional SearXNG categories param (e.g. news).",
                    },
                    "searx_base_url": {
                        "type": "string",
                        "description": "Override SearXNG base URL for this call (no trailing slash).",
                    },
                },
                "required": ["query"],
            },
        ),
        mcp_types.Tool(
            name="news_ingest_urls",
            description="Fetch Open Graph / title / meta description for each HTTP(S) URL.",
            inputSchema={
                "type": "object",
                "properties": {
                    "urls": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Article or page URLs.",
                    }
                },
                "required": ["urls"],
            },
        ),
        mcp_types.Tool(
            name="news_curate",
            description=(
                "Fetch enabled RSS feeds, optional SearXNG queries, and optional extra URLs; merge; "
                "deduplicate; return newest-first headlines as JSON. Per-source errors are collected "
                "without failing the whole batch."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "max_per_source": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100,
                        "default": 20,
                        "description": "Cap items taken from each RSS feed or each SearX query.",
                    },
                    "max_total": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 200,
                        "default": 60,
                        "description": "Max items after merge and deduplication.",
                    },
                    "searx_queries": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional SearXNG queries to merge (needs SEARXNG_BASE_URL or searx_base_url).",
                    },
                    "searx_base_url": {
                        "type": "string",
                        "description": "SearXNG base URL for this request.",
                    },
                    "searx_categories": {
                        "type": "string",
                        "description": "Optional categories param for every SearX query (e.g. news).",
                    },
                    "extra_urls": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional page URLs to ingest as web sources.",
                    },
                    "include_disabled_feeds": {
                        "type": "boolean",
                        "default": False,
                        "description": "If true, also fetch feeds marked enabled=false.",
                    },
                    "deduplicate": {
                        "type": "boolean",
                        "default": True,
                        "description": "If false, skip all deduplication.",
                    },
                    "dedupe_urls_only": {
                        "type": "boolean",
                        "default": False,
                        "description": "If true, only collapse exact canonical URLs (keep similar headlines).",
                    },
                    "min_title_fingerprint_len": {
                        "type": "integer",
                        "minimum": 8,
                        "maximum": 200,
                        "default": 24,
                        "description": "Minimum normalized title length for headline-based deduplication.",
                    },
                },
                "additionalProperties": False,
            },
        ),
    ]


def build_news_server() -> Server:
    store = FeedStore()

    server = Server(
        "mcp-news-server",
        version="0.1.0",
        instructions=_INSTRUCTIONS,
    )

    @server.list_tools()
    async def list_tools() -> list[mcp_types.Tool]:
        return build_tool_list()

    @server.call_tool()
    async def call_tool(name: str, arguments: dict | None) -> list[mcp_types.ContentBlock]:
        args = arguments or {}

        if name == "news_list_feeds":
            feeds = store.load()
            payload = {
                "dataDir": str(default_data_dir()),
                "feeds": [f.to_json_dict() for f in feeds],
            }
            return _json_text(payload)

        if name == "news_add_rss_feed":
            url = args.get("url")
            if not isinstance(url, str) or not url.strip():
                raise _err("Missing or invalid 'url'.")
            label = args.get("label")
            lab = str(label).strip() if isinstance(label, str) else ""
            store.add(url.strip(), lab)
            feeds = store.load()
            return _json_text({"ok": True, "feeds": [f.to_json_dict() for f in feeds]})

        if name == "news_remove_rss_feed":
            url = args.get("url")
            if not isinstance(url, str) or not url.strip():
                raise _err("Missing or invalid 'url'.")
            feeds = store.remove(url.strip())
            return _json_text({"ok": True, "feeds": [f.to_json_dict() for f in feeds]})

        if name == "news_searx_search":
            q = args.get("query")
            if not isinstance(q, str) or not q.strip():
                raise _err("Missing or invalid 'query'.")
            limit = _int(args, "limit", 15, min_v=1, max_v=50)
            categories = _optional_str(args, "categories")
            base = _optional_str(args, "searx_base_url") or _searx_base_from_env()
            if not base:
                raise _err("Set `searx_base_url` or environment variable `SEARXNG_BASE_URL`.")
            async with async_client() as client:
                items = await searx_search(
                    client, base, q.strip(), limit=limit, categories=categories
                )
            return _json_text({"query": q.strip(), "items": [i.to_json_dict() for i in items]})

        if name == "news_ingest_urls":
            urls = _str_list(args, "urls")
            if not urls:
                raise _err("'urls' must be a non-empty list.")
            if len(urls) > 40:
                raise _err("At most 40 URLs per call.")

            errors: list[dict[str, str]] = []

            async with async_client() as client:

                async def one(u: str) -> NewsItem | None:
                    try:
                        return await fetch_page_metadata(client, u)
                    except Exception as e:
                        errors.append({"url": u, "error": str(e) or type(e).__name__})
                        return None

                results = await asyncio.gather(*(one(u) for u in urls))

            items = [x for x in results if x is not None]
            return _json_text({"items": [i.to_json_dict() for i in items], "errors": errors})

        if name == "news_curate":
            max_per = _int(args, "max_per_source", 20, min_v=1, max_v=100)
            max_total = _int(args, "max_total", 60, min_v=1, max_v=200)
            searx_queries = _str_list(args, "searx_queries")
            extra_urls = _str_list(args, "extra_urls")
            if len(extra_urls) > 40:
                raise _err("At most 40 extra_urls.")
            include_disabled = _bool(args, "include_disabled_feeds", False)
            dedupe = _bool(args, "deduplicate", True)
            urls_only = _bool(args, "dedupe_urls_only", False)
            min_fp = _int(args, "min_title_fingerprint_len", 24, min_v=8, max_v=200)
            searx_base = _optional_str(args, "searx_base_url") or _searx_base_from_env()
            searx_cat = _optional_str(args, "searx_categories")

            feeds = store.load()
            if not include_disabled:
                feeds = [f for f in feeds if f.enabled]

            merged: list[NewsItem] = []
            errors: list[dict[str, str]] = []

            async with async_client() as client:

                async def rss_one(f_url: str, label: str) -> None:
                    try:
                        got = await fetch_rss_via_http(
                            client,
                            f_url,
                            feed_label=label,
                            max_items=max_per,
                        )
                        merged.extend(got)
                    except Exception as e:
                        errors.append({"source": f_url, "error": str(e) or type(e).__name__})

                await asyncio.gather(*(rss_one(f.url, f.label) for f in feeds))

                if searx_queries:
                    if not searx_base:
                        errors.append(
                            {
                                "source": "searx",
                                "error": "searx_queries provided but no SEARXNG_BASE_URL or searx_base_url",
                            }
                        )
                    else:

                        async def sx_one(sq: str) -> None:
                            try:
                                got = await searx_search(
                                    client,
                                    searx_base,
                                    sq,
                                    limit=max_per,
                                    categories=searx_cat,
                                )
                                merged.extend(got)
                            except Exception as e:
                                errors.append(
                                    {
                                        "source": f"searx:{sq}",
                                        "error": str(e) or type(e).__name__,
                                    }
                                )

                        await asyncio.gather(*(sx_one(sq) for sq in searx_queries))

                if extra_urls:

                    async def web_one(u: str) -> None:
                        try:
                            merged.append(await fetch_page_metadata(client, u))
                        except Exception as e:
                            errors.append({"source": u, "error": str(e) or type(e).__name__})

                    await asyncio.gather(*(web_one(u) for u in extra_urls))

            if dedupe:
                merged = dedupe_news_items(
                    merged,
                    dedupe_urls=True,
                    dedupe_titles=not urls_only,
                    min_title_fingerprint_len=min_fp,
                )
            else:
                merged.sort(
                    key=lambda it: (0, (it.published or "").strip())
                    if (it.published or "").strip()
                    else (1, ""),
                    reverse=True,
                )

            merged = merged[:max_total]
            payload = {
                "itemCount": len(merged),
                "items": [i.to_json_dict() for i in merged],
                "errors": errors,
                "meta": {
                    "rssFeedsUsed": len(feeds),
                    "searxQueries": searx_queries,
                    "extraUrls": len(extra_urls),
                    "deduplicated": dedupe,
                    "dedupeUrlsOnly": urls_only,
                },
            }
            return [mcp_types.TextContent(type="text", text=safe_json_dumps(payload))]

        raise McpError(
            mcp_types.ErrorData(
                code=mcp_types.METHOD_NOT_FOUND,
                message=f"Unknown tool {name!r}.",
            )
        )

    return server
