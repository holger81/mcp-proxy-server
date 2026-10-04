"""Characterization tests for FeedStore (PLAN.md PR 0.3).

These pin *today's* behavior as a regression baseline for the audit fixes.
Deliberately NOT characterized: DISABLE_URLS re-disable semantics (decision
D1 changes them) and dedupe/sort ordering (fix PRs assert the new behavior).
"""

from __future__ import annotations

import pytest

from mcp_news_server.feed_migrations import SUPPLEMENTAL_FEEDS
from mcp_news_server.models import FeedEntry
from mcp_news_server.store import FeedStore

SUPPLEMENTAL_URLS = {f.url for f in SUPPLEMENTAL_FEEDS}


@pytest.fixture
def store(tmp_path, monkeypatch) -> FeedStore:
    monkeypatch.setenv("NEWS_MCP_DATA_DIR", str(tmp_path))
    return FeedStore(data_dir=tmp_path)


def urls(feeds: list[FeedEntry]) -> list[str]:
    return [f.url for f in feeds]


def test_empty_store_creates_yaml_and_appends_supplementals(store: FeedStore, tmp_path):
    feeds = store.load()
    assert (tmp_path / "feeds.yaml").is_file()
    # Current behavior: every load appends the three supplemental feeds.
    assert SUPPLEMENTAL_URLS <= set(urls(feeds))


def test_add_load_remove_roundtrip(store: FeedStore):
    store.add("https://a.test/rss.xml", "Wire A")
    loaded = store.load()
    entry = next(f for f in loaded if f.url == "https://a.test/rss.xml")
    assert entry.label == "Wire A"
    assert entry.enabled is True

    kept = store.remove("https://a.test/rss.xml")
    assert "https://a.test/rss.xml" not in urls(kept)
    assert "https://a.test/rss.xml" not in urls(store.load())


def test_add_dedupes_canonically_equivalent_urls(store: FeedStore):
    store.add("https://www.b.test/feed/?utm_source=x", "First")
    store.add("https://b.test/feed/", "Second")
    b_feeds = [f for f in store.load() if "b.test" in f.url]
    assert len(b_feeds) == 1
    assert b_feeds[0].label == "First"


def test_changes_persist_across_instances(tmp_path, monkeypatch):
    monkeypatch.setenv("NEWS_MCP_DATA_DIR", str(tmp_path))
    FeedStore(data_dir=tmp_path).add("https://c.test/rss", "C")
    reloaded = FeedStore(data_dir=tmp_path).load()
    assert "https://c.test/rss" in urls(reloaded)


def test_save_load_preserves_label_and_disabled(store: FeedStore):
    store.save(
        [
            FeedEntry(url="https://d.test/rss", label="Disabled One", enabled=False),
            FeedEntry(url="https://e.test/rss", label="", enabled=True),
        ]
    )
    loaded = {f.url: f for f in store.load()}
    assert loaded["https://d.test/rss"].enabled is False
    assert loaded["https://d.test/rss"].label == "Disabled One"
    assert loaded["https://e.test/rss"].label == ""


def test_url_replacement_migration_applied_on_load(store: FeedStore):
    # The store's documented self-heal: dead default URL is replaced at load.
    store._path.write_text(
        "feeds:\n"
        "- url: https://www.sfchronicle.com/bay-area/feed/\n"
        "  label: Chronicle\n"
        "  enabled: true\n",
        encoding="utf-8",
    )
    loaded = store.load()
    assert "https://www.sfchronicle.com/bay-area/feed/" not in urls(loaded)
    assert "https://www.sfchronicle.com/rss/feed/Bay-Area-News-448.php" in urls(loaded)
