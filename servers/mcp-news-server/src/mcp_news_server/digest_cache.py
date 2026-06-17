from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp_news_server.dedupe import dedupe_news_items
from mcp_news_server.feed_regions import feeds_bay_area, feeds_germany, feeds_today_world
from mcp_news_server.fetchers import gather_rss_for_feeds
from mcp_news_server.http_util import async_client
from mcp_news_server.llm_curator import maybe_curate_digest_payload
from mcp_news_server.models import FeedEntry, NewsItem
from mcp_news_server.store import FeedStore

log = logging.getLogger(__name__)


def _refresh_interval_s() -> float:
    raw = os.environ.get("NEWS_MCP_CACHE_REFRESH_SECONDS", "").strip()
    if not raw:
        return 600.0
    try:
        return max(60.0, min(86_400.0, float(raw)))
    except ValueError:
        return 600.0


def _cache_max_per() -> int:
    raw = os.environ.get("NEWS_MCP_CACHE_MAX_PER_SOURCE", "").strip()
    if not raw:
        return 20
    try:
        return max(1, min(100, int(raw)))
    except ValueError:
        return 20


def _cache_max_total() -> int:
    raw = os.environ.get("NEWS_MCP_CACHE_MAX_TOTAL", "").strip()
    if not raw:
        return 60
    try:
        return max(1, min(200, int(raw)))
    except ValueError:
        return 60


def _min_fp() -> int:
    raw = os.environ.get("NEWS_MCP_CACHE_MIN_TITLE_FP_LEN", "").strip()
    if not raw:
        return 24
    try:
        return max(8, min(200, int(raw)))
    except ValueError:
        return 24


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _searx_base_from_env() -> str | None:
    v = os.environ.get("SEARXNG_BASE_URL", "").strip()
    return v or None


def _local_searx_queries() -> list[str]:
    raw = os.environ.get("NEWS_MCP_LOCAL_SEARX_QUERIES", "").strip()
    if not raw:
        return []
    out: list[str] = []
    for part in raw.split(";"):
        s = part.strip().strip('"').strip("'").strip()
        if s:
            out.append(s)
    return out[:5]


def _local_searx_categories() -> str | None:
    v = os.environ.get("NEWS_MCP_LOCAL_SEARX_CATEGORIES", "").strip()
    return v or None


class DigestCache:
    """Persists pre-merged 'today' and 'Germany' digests; refreshed on a timer."""

    def __init__(self, store: FeedStore) -> None:
        self._store = store
        self._dir = store.data_dir / "cache"
        self._today_path = self._dir / "today.json"
        self._germany_path = self._dir / "germany.json"
        self._local_path = self._dir / "local.json"
        self._lock = asyncio.Lock()
        self._today_payload: dict[str, Any] = self._load_disk(self._today_path, "today")
        self._germany_payload: dict[str, Any] = self._load_disk(
            self._germany_path, "germany"
        )
        self._local_payload: dict[str, Any] = self._load_disk(self._local_path, "local")

    def _load_disk(self, path: Path, digest: str) -> dict[str, Any]:
        if not path.is_file():
            return _empty_payload(digest, None)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return _empty_payload(digest, None)
            data.setdefault("meta", {})
            if isinstance(data["meta"], dict):
                data["meta"].setdefault("digest", digest)
            return data
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Could not load digest cache %s: %s", path, e)
            return _empty_payload(digest, None)

    def _write_disk(self, path: Path, payload: dict[str, Any]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        tmp.replace(path)

    def snapshot_today(self) -> dict[str, Any]:
        return json.loads(json.dumps(self._today_payload, default=str))

    def snapshot_germany(self) -> dict[str, Any]:
        return json.loads(json.dumps(self._germany_payload, default=str))

    def snapshot_local(self) -> dict[str, Any]:
        return json.loads(json.dumps(self._local_payload, default=str))

    async def refresh_today(self) -> None:
        async with self._lock:
            await self._refresh_today_unlocked()

    async def refresh_germany(self) -> None:
        async with self._lock:
            await self._refresh_germany_unlocked()

    async def refresh_local(self) -> None:
        async with self._lock:
            await self._refresh_local_unlocked()

    async def refresh_all(self) -> None:
        async with self._lock:
            await self._refresh_today_unlocked()
            await self._refresh_germany_unlocked()
            await self._refresh_local_unlocked()

    async def _refresh_today_unlocked(self) -> None:
        feeds = feeds_today_world(self._store.load())
        self._today_payload = await self._build_payload("today", feeds)

    async def _refresh_germany_unlocked(self) -> None:
        feeds = feeds_germany(self._store.load())
        self._germany_payload = await self._build_payload("germany", feeds)

    async def _refresh_local_unlocked(self) -> None:
        feeds = feeds_bay_area(self._store.load())
        self._local_payload = await self._build_payload("local", feeds)

    async def _build_payload(
        self, digest: str, feeds: list[FeedEntry]
    ) -> dict[str, Any]:
        max_per = _cache_max_per()
        max_total = _cache_max_total()
        min_fp = _min_fp()
        errors: list[dict[str, str]] = []
        merged: list[NewsItem] = []
        if not feeds:
            errors.append(
                {
                    "source": digest,
                    "error": "No feeds in this digest (check labels or enable feeds).",
                }
            )
            pl = _finalize_payload([], errors, digest, max_total, min_fp, feed_count=0)
            self._write_disk(
                _path_for_digest(self, digest),
                pl,
            )
            return pl

        async with async_client() as client:
            merged = await gather_rss_for_feeds(
                client,
                feeds,
                max_per=max_per,
                errors=errors,
            )

            if digest == "local":
                base = _searx_base_from_env()
                qs = _local_searx_queries()
                if base and qs:
                    from mcp_news_server.fetchers import searx_search

                    cats = _local_searx_categories()

                    async def one(q: str) -> list[NewsItem]:
                        try:
                            return await searx_search(
                                client,
                                base,
                                q,
                                limit=max_per,
                                categories=cats,
                            )
                        except Exception as e:
                            errors.append(
                                {
                                    "source": f"searx:{q}",
                                    "error": str(e) or type(e).__name__,
                                }
                            )
                            return []

                    batches = await asyncio.gather(*(one(q) for q in qs))
                    for b in batches:
                        merged.extend(b)

        pl = _finalize_payload(
            merged, errors, digest, max_total, min_fp, feed_count=len(feeds)
        )
        pl = await maybe_curate_digest_payload(pl, digest=digest, client=client)
        self._write_disk(
            _path_for_digest(self, digest),
            pl,
        )
        return pl

    async def run_periodic_refresh(self) -> None:
        interval = _refresh_interval_s()
        while True:
            try:
                await self.refresh_all()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("digest refresh failed")
            await asyncio.sleep(interval)


def _path_for_digest(cache: DigestCache, digest: str) -> Path:
    if digest == "today":
        return cache._today_path
    if digest == "germany":
        return cache._germany_path
    return cache._local_path


def _finalize_payload(
    merged: list[NewsItem],
    errors: list[dict[str, str]],
    digest: str,
    max_total: int,
    min_fp: int,
    *,
    feed_count: int,
) -> dict[str, Any]:
    out = dedupe_news_items(
        merged,
        dedupe_urls=True,
        dedupe_titles=True,
        min_title_fingerprint_len=min_fp,
    )
    out = out[:max_total]
    now = _utc_now_iso()
    return {
        "itemCount": len(out),
        "items": [i.to_json_dict() for i in out],
        "errors": errors,
        "meta": {
            "digest": digest,
            "source": "cache",
            "updatedAt": now,
            "rssFeedsUsed": feed_count,
            "cacheRefreshIntervalSeconds": int(_refresh_interval_s()),
            "deduplicated": True,
        },
    }


def _empty_payload(digest: str, updated_at: str | None) -> dict[str, Any]:
    return {
        "itemCount": 0,
        "items": [],
        "errors": [],
        "meta": {
            "digest": digest,
            "source": "cache",
            "updatedAt": updated_at,
            "rssFeedsUsed": 0,
            "cacheRefreshIntervalSeconds": int(_refresh_interval_s()),
            "deduplicated": True,
        },
    }
