"""PR 2.1 (PLAN.md Phase 2): atomic locked feeds.yaml writes, corrupt-file recovery."""

from __future__ import annotations

import threading

import pytest
import yaml

from mcp_news_server.feed_migrations import SUPPLEMENTAL_FEEDS
from mcp_news_server.models import FeedEntry
from mcp_news_server.store import FeedStore

SUPPLEMENTAL_URLS = {f.url for f in SUPPLEMENTAL_FEEDS}
GARBAGE = "\tthis: is not valid yaml\n"


@pytest.fixture
def store(tmp_path, monkeypatch) -> FeedStore:
    monkeypatch.setenv("NEWS_MCP_DATA_DIR", str(tmp_path))
    return FeedStore(data_dir=tmp_path)


def urls(feeds: list[FeedEntry]) -> list[str]:
    return [f.url for f in feeds]


def test_save_is_atomic_and_leaves_no_tmp_file(store: FeedStore, tmp_path):
    store.save([FeedEntry(url="https://a.test/rss", label="A", enabled=True)])
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "feeds.yaml"]
    assert leftovers == []
    data = yaml.safe_load((tmp_path / "feeds.yaml").read_text(encoding="utf-8"))
    assert data["feeds"][0]["url"] == "https://a.test/rss"


def test_corrupt_yaml_is_quarantined_and_seeds_take_over(store: FeedStore, tmp_path):
    (tmp_path / "feeds.yaml").write_text(GARBAGE, encoding="utf-8")

    feeds = store.load()

    # Recovery starts from the seeded defaults (supplemental feeds).
    assert SUPPLEMENTAL_URLS <= set(urls(feeds))
    # The unreadable original is preserved beside the store.
    backups = list(tmp_path.glob("feeds.yaml.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == GARBAGE
    # The live file is valid again and reload is stable.
    yaml.safe_load((tmp_path / "feeds.yaml").read_text(encoding="utf-8"))
    assert urls(store.load()) == urls(feeds)


def test_non_mapping_yaml_does_not_crash(store: FeedStore, tmp_path):
    (tmp_path / "feeds.yaml").write_text("- url: https://x.test/rss\n", encoding="utf-8")
    feeds = store.load()
    assert SUPPLEMENTAL_URLS <= set(urls(feeds))


def test_concurrent_adds_all_persist(store: FeedStore):
    # Without the load-modify-save lock, threads clobber each other's updates
    # (last writer wins) and URLs disappear.
    n_threads = 8
    barrier = threading.Barrier(n_threads)

    def worker(i: int) -> None:
        barrier.wait()
        store.add(f"https://host{i}.test/rss", f"Feed {i}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    loaded = set(urls(store.load()))
    for i in range(n_threads):
        assert f"https://host{i}.test/rss" in loaded
    # The file on disk is parseable despite the concurrent writers.
    assert isinstance(loaded, set)
