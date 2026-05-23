"""Background refresh for mcp-news-server digest caches (today / germany / local).

The news stdio server only runs while the proxy handles an upstream tool call, so its
in-process 10-minute timer never ticks. This task runs inside the proxy container and
writes ``/data/mcp-news/cache/*.json`` on a schedule.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from pathlib import Path

from mcp_proxy.config_store import ServerConfigStore
from mcp_proxy.settings import Settings

_log = logging.getLogger("mcp_proxy.news_refresh")

_MCP_NEWS_SERVER_ID = "mcp-news"


def resolve_news_data_dir(settings: Settings, server_store: ServerConfigStore) -> Path:
    if settings.news_mcp_data_dir is not None:
        return settings.news_mcp_data_dir.expanduser().resolve()
    srv = server_store.get(_MCP_NEWS_SERVER_ID)
    if srv and srv.env:
        raw = (srv.env.get("NEWS_MCP_DATA_DIR") or "").strip()
        if raw:
            return Path(raw).expanduser().resolve()
    return (settings.data_dir / "mcp-news").resolve()


def news_refresh_should_run(
    data_dir: Path, server_store: ServerConfigStore
) -> bool:
    srv = server_store.get(_MCP_NEWS_SERVER_ID)
    if srv is not None and srv.enabled:
        return True
    return (data_dir / "feeds.yaml").is_file()


class NewsDigestRefresher:
    """Periodic ``DigestCache.refresh_all()`` loop."""

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._task: asyncio.Task[None] | None = None

    @classmethod
    def try_start(
        cls, settings: Settings, server_store: ServerConfigStore
    ) -> NewsDigestRefresher | None:
        if not settings.news_digest_refresh_enabled:
            _log.info("News digest background refresh is disabled")
            return None
        try:
            from mcp_news_server.digest_cache import (  # noqa: PLC0415
                DigestCache,
                _refresh_interval_s,
            )
            from mcp_news_server.store import FeedStore  # noqa: PLC0415
        except ImportError:
            _log.warning(
                "mcp-news-server is not installed; background news digest refresh disabled"
            )
            return None

        data_dir = resolve_news_data_dir(settings, server_store)
        if not news_refresh_should_run(data_dir, server_store):
            _log.info(
                "News digest background refresh skipped (no enabled %r server and no %s)",
                _MCP_NEWS_SERVER_ID,
                data_dir / "feeds.yaml",
            )
            return None

        refresher = cls(data_dir)
        interval = _refresh_interval_s()
        _log.info(
            "News digest background refresh starting (data_dir=%s, interval_s=%s)",
            data_dir,
            int(interval),
        )

        async def _loop() -> None:
            store = FeedStore(data_dir)
            cache = DigestCache(store)
            while True:
                try:
                    await cache.refresh_all()
                    _log.info("News digest background refresh completed")
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _log.exception("News digest background refresh failed")
                await asyncio.sleep(interval)

        refresher._task = asyncio.create_task(_loop())
        return refresher

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        _log.info("News digest background refresh stopped")
