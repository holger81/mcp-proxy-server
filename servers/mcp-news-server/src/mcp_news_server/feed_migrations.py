"""One-time URL fixes for bundled feeds (existing installs keep feeds.yaml on disk)."""

from __future__ import annotations

from mcp_news_server.models import FeedEntry

# Old default URLs that no longer work → replacements (or disable when no replacement).
URL_REPLACEMENTS: dict[str, str] = {
    "https://www.sfchronicle.com/bay-area/feed/": (
        "https://www.sfchronicle.com/rss/feed/Bay-Area-News-448.php"
    ),
}

DISABLE_URLS: frozenset[str] = frozenset(
    {
        "https://www.mercurynews.com/feed/",
        "https://www.eastbaytimes.com/feed/",
    }
)

# Added when missing (working Bay Area / San Jose sources).
SUPPLEMENTAL_FEEDS: list[FeedEntry] = [
    FeedEntry(
        url="https://www.nbcbayarea.com/?rss=y",
        label="[Bay Area] NBC Bay Area",
        enabled=True,
    ),
    FeedEntry(
        url="https://abc7news.com/feed/",
        label="[Bay Area] ABC7 Bay Area",
        enabled=True,
    ),
    FeedEntry(
        url="https://www.ktvu.com/rss.xml",
        label="[Bay Area] KTVU Fox 2",
        enabled=True,
    ),
]


def migrate_feeds(feeds: list[FeedEntry]) -> tuple[list[FeedEntry], bool]:
    """Apply URL replacements, disable dead feeds, append supplemental sources."""
    out: list[FeedEntry] = []
    changed = False
    seen: set[str] = set()

    for f in feeds:
        url = f.url.strip()
        if url in URL_REPLACEMENTS:
            url = URL_REPLACEMENTS[url]
            changed = True
        if url in DISABLE_URLS:
            if f.enabled:
                f = FeedEntry(url=url, label=f.label, enabled=False)
                changed = True
        if url in seen:
            continue
        seen.add(url)
        if f.url != url:
            f = FeedEntry(url=url, label=f.label, enabled=f.enabled)
        out.append(f)

    for extra in SUPPLEMENTAL_FEEDS:
        if extra.url not in seen:
            out.append(extra)
            seen.add(extra.url)
            changed = True

    return out, changed
