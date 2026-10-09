"""One-time feed list migrations (each recorded in feeds.yaml, so it runs once).

Applied migration IDs are persisted under ``migrations_applied`` in
feeds.yaml. That makes user intent sticky: deleted feeds are not resurrected
and re-enabled feeds stay enabled (decision D1).
"""

from __future__ import annotations

from typing import Iterable

from mcp_news_server.models import FeedEntry

# Old default URLs that no longer work → replacements (or disable when no replacement).
URL_REPLACEMENTS: dict[str, str] = {
    "https://www.sfchronicle.com/bay-area/feed/": (
        "https://www.sfchronicle.com/rss/feed/Bay-Area-News-448.php"
    ),
    # KQED retired /news/feed/rss (404); the legacy host still serves it.
    "https://www.kqed.org/news/feed/rss": "https://ww2.kqed.org/news/feed/",
    # DW renamed the Germany feed; the old name answers 200 + "no feed by that name".
    "https://rss.dw.com/rdf/rss-en-germany": "https://rss.dw.com/rdf/rss-en-ger",
}

DISABLE_URLS: frozenset[str] = frozenset(
    {
        "https://www.mercurynews.com/feed/",
        "https://www.eastbaytimes.com/feed/",
        # Chronicle bot-blocks every RSS fetch from this server (403 even with a
        # browser User-Agent; Akamai fingerprinting).
        "https://www.sfchronicle.com/rss/feed/Bay-Area-News-448.php",
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


def migrate_feeds(
    feeds: list[FeedEntry], applied: Iterable[str] = ()
) -> tuple[list[FeedEntry], list[str], bool]:
    """Apply every migration whose ID is not in ``applied`` yet.

    Each migration is recorded per item (``kind:<url>``), so a future release
    only needs to add new entries to run for installs that lack them. Returns
    ``(feeds, newly_recorded_ids, changed)``; the caller persists the union of
    ``applied`` and the newly recorded IDs alongside the feed list.
    """
    handled = set(applied)
    recorded: list[str] = []

    def once(migration_id: str) -> bool:
        if migration_id in handled:
            return False
        handled.add(migration_id)
        recorded.append(migration_id)
        return True

    changed = False
    out: list[FeedEntry] = []
    seen: set[str] = set()

    for f in feeds:
        url = f.url.strip()

        rid = f"url-replacement:{url}"
        if url in URL_REPLACEMENTS and once(rid):
            url = URL_REPLACEMENTS[url]
            changed = True

        rid = f"disable-dead-feed:{url}"
        if url in DISABLE_URLS and once(rid) and f.enabled:
            f = FeedEntry(url=url, label=f.label, enabled=False)
            changed = True

        if url in seen:
            continue
        seen.add(url)
        if f.url != url:
            f = FeedEntry(url=url, label=f.label, enabled=f.enabled)
        out.append(f)

    for extra in SUPPLEMENTAL_FEEDS:
        if once(f"supplemental-feed:{extra.url}") and extra.url not in seen:
            out.append(extra)
            seen.add(extra.url)
            changed = True

    return out, recorded, changed
