"""Tests for digest_scope normalization on news_curate / news_briefing."""

from __future__ import annotations

import pytest

from mcp_news_server.models import FeedEntry
from mcp_news_server.server import _digest_scope, _filter_feeds_for_scope


def test_digest_scope_defaults_to_global() -> None:
    assert _digest_scope({}) == "global"


def test_digest_scope_accepts_global_local_germany_full() -> None:
    assert _digest_scope({"digest_scope": "local"}) == "local"
    assert _digest_scope({"digest_scope": "germany"}) == "germany"
    assert _digest_scope({"digest_scope": "full"}) == "full"
    assert _digest_scope({"digest_scope": "global"}) == "global"


def test_digest_scope_today_alias_and_scope_key() -> None:
    assert _digest_scope({"digest_scope": "today"}) == "global"
    assert _digest_scope({"scope": "local"}) == "local"


def test_digest_scope_rejects_unknown() -> None:
    with pytest.raises(Exception, match="digest_scope"):
        _digest_scope({"digest_scope": "mars"})


def test_filter_feeds_for_scope() -> None:
    feeds = [
        FeedEntry(url="https://tagesschau.de/rss", label="[Germany] Tagesschau"),
        FeedEntry(url="https://www.sfchronicle.com/rss", label="[Bay Area] Chronicle"),
        FeedEntry(url="https://example.com/world.rss", label="World"),
    ]
    global_feeds = _filter_feeds_for_scope(feeds, "global")
    assert len(global_feeds) == 2
    assert all("tagesschau" not in f.url for f in global_feeds)

    germany_feeds = _filter_feeds_for_scope(feeds, "germany")
    assert len(germany_feeds) == 1
    assert "tagesschau" in germany_feeds[0].url

    local_feeds = _filter_feeds_for_scope(feeds, "local")
    assert len(local_feeds) == 1
    assert "sfchronicle" in local_feeds[0].url

    assert len(_filter_feeds_for_scope(feeds, "full")) == 3
