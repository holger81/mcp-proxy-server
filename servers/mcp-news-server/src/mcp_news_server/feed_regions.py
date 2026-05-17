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

_LOCAL_LABEL_MARKERS = ("[local]",)
_BAY_AREA_LABEL_MARKERS = ("[bay area]", "bay area —", "bay area -")
_SAN_JOSE_LABEL_MARKERS = ("[san jose]", "san jose —", "san jose -")
_EVERGREEN_LABEL_MARKERS = ("[evergreen]", "evergreen —", "evergreen -")

_BAY_AREA_URL_HINTS = (
    "sfchronicle.com",
    "kqed.org",
    "sanjosespotlight.com",
    "nbcbayarea.com",
    "abc7news.com",
    "ktvu.com",
    "calmatters.org",
    "bay-area",
)


def feed_is_germany(f: FeedEntry) -> bool:
    """Heuristic: label tag, or known DE-focused feed URLs."""
    lab = (f.label or "").strip().lower()
    for m in _GERMANY_LABEL_MARKERS:
        if m in lab:
            return True
    u = (f.url or "").lower()
    return any(h in u for h in _GERMANY_URL_HINTS)

def feed_is_bay_area(f: FeedEntry) -> bool:
    """Heuristic: label tag, or Bay Area outlet URL hints."""
    lab = (f.label or "").strip().lower()
    if any(m in lab for m in _LOCAL_LABEL_MARKERS):
        return True
    if any(m in lab for m in _BAY_AREA_LABEL_MARKERS):
        return True
    if any(m in lab for m in _SAN_JOSE_LABEL_MARKERS):
        return True
    if any(m in lab for m in _EVERGREEN_LABEL_MARKERS):
        return True
    u = (f.url or "").lower()
    return any(h in u for h in _BAY_AREA_URL_HINTS)


def feed_is_san_jose(f: FeedEntry) -> bool:
    """Heuristic: explicit label tag; URL hints are intentionally conservative."""
    lab = (f.label or "").strip().lower()
    if any(m in lab for m in _SAN_JOSE_LABEL_MARKERS):
        return True
    if any(m in lab for m in _EVERGREEN_LABEL_MARKERS):
        return True
    return False


def feed_is_evergreen_sanjose(f: FeedEntry) -> bool:
    """Heuristic: explicit label tag only (Evergreen is a local neighborhood/area)."""
    lab = (f.label or "").strip().lower()
    return any(m in lab for m in _EVERGREEN_LABEL_MARKERS)


def feeds_today_world(all_feeds: list[FeedEntry]) -> list[FeedEntry]:
    """Non-Germany feeds (world / US / regional outside DE-only bucket)."""
    return [f for f in all_feeds if f.enabled and not feed_is_germany(f)]


def feeds_germany(all_feeds: list[FeedEntry]) -> list[FeedEntry]:
    """Germany-focused feeds only."""
    return [f for f in all_feeds if f.enabled and feed_is_germany(f)]


def feeds_bay_area(all_feeds: list[FeedEntry]) -> list[FeedEntry]:
    """Bay Area / local outlets subset (includes San Jose / Evergreen-labelled feeds)."""
    return [f for f in all_feeds if f.enabled and feed_is_bay_area(f)]


def feeds_san_jose(all_feeds: list[FeedEntry]) -> list[FeedEntry]:
    """San Jose subset (label-driven)."""
    return [f for f in all_feeds if f.enabled and feed_is_san_jose(f)]


def feeds_evergreen_sanjose(all_feeds: list[FeedEntry]) -> list[FeedEntry]:
    """Evergreen (San Jose) subset (label-driven)."""
    return [f for f in all_feeds if f.enabled and feed_is_evergreen_sanjose(f)]
