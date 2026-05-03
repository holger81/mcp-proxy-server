from __future__ import annotations

from mcp_news_server.models import FeedEntry

_GERMANY_LABEL_MARKERS = ("[germany]", "germany —", "germany -")
_GERMANY_URL_HINTS = (
    "tagesschau.de",
    "rss-en-germany",
    "rss.deutschland",
    "thema/deutschland",
    "germany/index~rss",
)


def feed_is_germany(f: FeedEntry) -> bool:
    """Heuristic: label tag, or known DE-focused feed URLs."""
    lab = (f.label or "").strip().lower()
    for m in _GERMANY_LABEL_MARKERS:
        if m in lab:
            return True
    u = (f.url or "").lower()
    return any(h in u for h in _GERMANY_URL_HINTS)


def feeds_today_world(all_feeds: list[FeedEntry]) -> list[FeedEntry]:
    """Non-Germany feeds (world / US / regional outside DE-only bucket)."""
    return [f for f in all_feeds if f.enabled and not feed_is_germany(f)]


def feeds_germany(all_feeds: list[FeedEntry]) -> list[FeedEntry]:
    """Germany-focused feeds only."""
    return [f for f in all_feeds if f.enabled and feed_is_germany(f)]
