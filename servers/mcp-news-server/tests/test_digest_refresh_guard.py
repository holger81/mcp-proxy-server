"""PR 2.3 (PLAN.md Phase 2): a total refresh failure must not wipe a good digest.

On main, a refresh where every feed errored produced an empty payload that
overwrote the last good digest in memory and on disk. The guard keeps the
previous payload and attaches the fresh errors instead.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
import respx

from mcp_news_server.digest_cache import DigestCache
from mcp_news_server.store import FeedStore

WIRE_RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>Wire</title>
<item><title>Alpha happens</title><link>https://wire.test/a</link><pubDate>Mon, 01 Sep 2025 09:00:00 GMT</pubDate></item>
<item><title>Beta follows</title><link>https://wire.test/b</link><pubDate>Mon, 01 Sep 2025 08:00:00 GMT</pubDate></item>
</channel></rss>"""


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("NEWS_MCP_DATA_DIR", str(tmp_path))
    (tmp_path / "feeds.yaml").write_text(
        "feeds:\n- url: https://wire.test/rss.xml\n  label: Wire\n  enabled: true\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("NEWS_MCP_LLM_MODEL", raising=False)
    monkeypatch.delenv("SEARXNG_BASE_URL", raising=False)
    return tmp_path


def _routes(wire_ok: bool):
    respx.get("https://wire.test/rss.xml").mock(
        return_value=httpx.Response(
            200 if wire_ok else 500,
            content=WIRE_RSS.encode() if wire_ok else b"down",
        )
    )
    respx.route().mock(return_value=httpx.Response(500))  # supplemental feeds


def test_total_failure_keeps_previous_digest(data_dir: Path):
    with respx.mock:
        _routes(wire_ok=True)
        cache = DigestCache(FeedStore(data_dir=data_dir))
        asyncio.run(cache.refresh_today())
        good = cache.snapshot_today()
        assert good["itemCount"] == 2

    with respx.mock:
        _routes(wire_ok=False)
        asyncio.run(cache.refresh_today())
        after = cache.snapshot_today()

    # Items survive...
    assert after["itemCount"] == 2
    assert [i["title"] for i in after["items"]] == [
        i["title"] for i in good["items"]
    ]
    # ...the failed refresh's errors are attached...
    assert after["errors"], "fresh errors were not attached"
    assert any("wire.test" in e.get("source", "") for e in after["errors"])
    # ...and the payload is honestly still the old one.
    assert after["meta"]["updatedAt"] == good["meta"]["updatedAt"]
    # Disk copy still has the items.
    disk = json.loads((data_dir / "cache" / "today.json").read_text(encoding="utf-8"))
    assert disk["itemCount"] == 2


def test_recovery_overwrites_kept_payload(data_dir: Path):
    with respx.mock:
        _routes(wire_ok=True)
        cache = DigestCache(FeedStore(data_dir=data_dir))
        asyncio.run(cache.refresh_today())

    with respx.mock:
        _routes(wire_ok=False)
        asyncio.run(cache.refresh_today())

    with respx.mock:
        _routes(wire_ok=True)
        asyncio.run(cache.refresh_today())
        recovered = cache.snapshot_today()

    assert recovered["itemCount"] == 2

    # The catch-all keeps erroring for the supplemental feeds; what matters is
    # that the wire feed is no longer in the error list.
    assert all("wire.test" not in e["source"] for e in recovered["errors"])


def test_starting_empty_stays_empty_on_failure(data_dir: Path):
    # Nothing good ever existed: an all-failed refresh stays an empty payload
    # (with errors) — the guard only protects existing items.
    with respx.mock:
        _routes(wire_ok=False)
        cache = DigestCache(FeedStore(data_dir=data_dir))
        asyncio.run(cache.refresh_today())
        payload = cache.snapshot_today()
    assert payload["itemCount"] == 0
    assert payload["errors"]
