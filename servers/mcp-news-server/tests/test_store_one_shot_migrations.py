"""PR 2.2 (PLAN.md Phase 2): one-shot migrations persisted in feeds.yaml.

Core guarantees: deleted feeds are not resurrected, a re-enabled dead feed
stays enabled (decision D1), and each migration records its ID per item.
"""

from __future__ import annotations

import pytest
import yaml

from mcp_news_server.models import FeedEntry
from mcp_news_server.store import FeedStore

KTVU = "https://www.ktvu.com/rss.xml"
MERCURY = "https://www.mercurynews.com/feed/"
OLD_CHRONICLE = "https://www.sfchronicle.com/bay-area/feed/"
NEW_CHRONICLE = "https://www.sfchronicle.com/rss/feed/Bay-Area-News-448.php"
KQED_OLD = "https://www.kqed.org/news/feed/rss"
KQED_NEW = "https://ww2.kqed.org/news/feed/"
DW_OLD = "https://rss.dw.com/rdf/rss-en-germany"
DW_NEW = "https://rss.dw.com/rdf/rss-en-ger"


@pytest.fixture
def store(tmp_path, monkeypatch) -> FeedStore:
    monkeypatch.setenv("NEWS_MCP_DATA_DIR", str(tmp_path))
    return FeedStore(data_dir=tmp_path)


def urls(feeds: list[FeedEntry]) -> list[str]:
    return [f.url for f in feeds]


def test_fresh_load_records_supplemental_migrations(store: FeedStore, tmp_path):
    loaded = store.load()
    assert KTVU in urls(loaded)
    data = yaml.safe_load((tmp_path / "feeds.yaml").read_text(encoding="utf-8"))
    applied = data["migrations_applied"]
    assert applied.count("supplemental-feed:" + KTVU) == 1
    assert sum(1 for m in applied if m.startswith("supplemental-feed:")) == 3


def test_deleted_supplemental_feed_stays_deleted(store: FeedStore):
    store.load()  # appends the supplemental feeds and records the migration
    store.remove(KTVU)

    assert KTVU not in urls(store.load())
    # even for a brand-new store instance reading only the file
    assert KTVU not in urls(FeedStore(data_dir=store.data_dir).load())


def test_reenabled_dead_feed_stays_enabled(store: FeedStore):
    # First migration run disables the dead feed once (fresh instance + save
    # without a prior load persist an empty migration list, like pre-2.2 files).
    store.save([FeedEntry(url=MERCURY, label="Mercury", enabled=True)])
    loaded = store.load()
    assert next(f for f in loaded if f.url == MERCURY).enabled is False

    # User re-enables it: the one-shot migration must not clobber that again.
    feeds = [
        FeedEntry(url=f.url, label=f.label, enabled=True) if f.url == MERCURY else f
        for f in loaded
    ]
    store.save(feeds)

    assert next(f for f in store.load() if f.url == MERCURY).enabled is True
    fresh = FeedStore(data_dir=store.data_dir).load()
    assert next(f for f in fresh if f.url == MERCURY).enabled is True


def test_url_replacement_is_one_shot(store: FeedStore):
    store._path.write_text(
        f"feeds:\n- url: {OLD_CHRONICLE}\n  label: Chronicle\n  enabled: true\n",
        encoding="utf-8",
    )
    loaded = store.load()
    assert OLD_CHRONICLE not in urls(loaded)
    assert NEW_CHRONICLE in urls(loaded)

    # Deliberate later re-add of the old URL is left alone (migration applied).
    store.add(OLD_CHRONICLE, "Legacy")
    assert OLD_CHRONICLE in urls(store.load())


def test_dead_feed_url_replacements(store: FeedStore):
    # PR 42: KQED moved to the legacy host, DW renamed its Germany feed.
    store._path.write_text(
        "feeds:\n"
        f"- url: {KQED_OLD}\n  label: KQED\n  enabled: true\n"
        f"- url: {DW_OLD}\n  label: DW Germany\n  enabled: true\n",
        encoding="utf-8",
    )
    loaded = store.load()
    got = urls(loaded)
    assert KQED_OLD not in got and KQED_NEW in got
    assert DW_OLD not in got and DW_NEW in got
    data = yaml.safe_load((store.data_dir / "feeds.yaml").read_text(encoding="utf-8"))
    assert f"url-replacement:{KQED_OLD}" in data["migrations_applied"]
    assert f"url-replacement:{DW_OLD}" in data["migrations_applied"]


def test_blocked_chronicle_feed_disabled_once(store: FeedStore):
    # PR 42: SF Chronicle bot-blocks RSS (403 for every fetch from this server).
    store.save([FeedEntry(url=NEW_CHRONICLE, label="Chronicle", enabled=True)])
    loaded = store.load()
    assert next(f for f in loaded if f.url == NEW_CHRONICLE).enabled is False

    # User re-enables it (same semantics as decision D1): stays enabled.
    store.save(
        [
            FeedEntry(url=f.url, label=f.label, enabled=True)
            if f.url == NEW_CHRONICLE
            else f
            for f in loaded
        ]
    )
    assert next(f for f in store.load() if f.url == NEW_CHRONICLE).enabled is True


def test_new_default_urls_pass_region_heuristics():
    # Digest scoping must still classify the replacement URLs correctly.
    from mcp_news_server.feed_regions import feed_is_bay_area, feed_is_germany

    assert feed_is_bay_area(FeedEntry(url=KQED_NEW, label="", enabled=True))
    assert feed_is_germany(FeedEntry(url=DW_NEW, label="", enabled=True))
    # prefix hint also keeps matching the old feed name for custom stores
    assert feed_is_germany(FeedEntry(url=DW_OLD, label="", enabled=True))
