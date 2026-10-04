"""PR 1.1 regression: LLM curator must run during digest refresh.

On main, `maybe_curate_digest_payload` ran after the shared httpx client was
closed, so the curator ALWAYS failed (meta.llmCurated=False with a closed
client error). These tests fail on the old code and pass on the fix.
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

LLM_CONTENT = json.dumps(
    {
        "briefing": "**Top theme** briefing.",
        "selected": [
            {"index": 2, "importance": "Beta matters most"},
            {"index": 1, "importance": "Alpha context"},
        ],
    }
)

LLM_URL = "https://llm.test/v1/chat/completions"


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("NEWS_MCP_DATA_DIR", str(tmp_path))
    (tmp_path / "feeds.yaml").write_text(
        "feeds:\n- url: https://wire.test/rss.xml\n  label: Wire\n  enabled: true\n",
        encoding="utf-8",
    )
    # Curator env: local base URL (mocked), explicit digests.
    monkeypatch.setenv("NEWS_MCP_LLM_MODEL", "test-curator")
    monkeypatch.setenv("NEWS_MCP_LLM_BASE_URL", "https://llm.test/v1")
    monkeypatch.setenv("NEWS_MCP_LLM_API_KEY", "test-key")
    monkeypatch.delenv("NEWS_MCP_LLM_ENABLED", raising=False)
    monkeypatch.delenv("SEARXNG_BASE_URL", raising=False)
    return tmp_path


def _mock_endpoints():
    # respx matches in registration order: specific first, catch-all last.
    llm_route = respx.post(LLM_URL).mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": LLM_CONTENT}}]}
        )
    )
    respx.get("https://wire.test/rss.xml").mock(
        return_value=httpx.Response(200, content=WIRE_RSS.encode())
    )
    respx.route().mock(return_value=httpx.Response(500))  # supplemental feeds
    return llm_route


def test_refresh_today_runs_curator_on_open_client(data_dir: Path):
    with respx.mock:
        llm_route = _mock_endpoints()
        cache = DigestCache(FeedStore(data_dir=data_dir))
        asyncio.run(cache.refresh_today())
        assert llm_route.called, "LLM endpoint was never called"
        payload = cache.snapshot_today()

    meta = payload["meta"]
    assert meta["llmCurated"] is True, f"curator did not run: {meta.get('llmError')}"
    assert meta["llmModel"] == "test-curator"
    assert payload["briefing"] == "**Top theme** briefing."
    # Curated order follows the LLM selection (Beta first), raw count preserved.
    assert [i["title"] for i in payload["items"]] == ["Beta follows", "Alpha happens"]
    assert meta["rawItemCount"] == 2


def test_llm_failure_is_non_fatal_and_flagged(data_dir: Path):
    with respx.mock:
        respx.post(LLM_URL).mock(return_value=httpx.Response(500))
        respx.get("https://wire.test/rss.xml").mock(
            return_value=httpx.Response(200, content=WIRE_RSS.encode())
        )
        respx.route().mock(return_value=httpx.Response(500))
        cache = DigestCache(FeedStore(data_dir=data_dir))
        asyncio.run(cache.refresh_today())
        payload = cache.snapshot_today()

    meta = payload["meta"]
    assert meta["llmCurated"] is False
    assert "llmError" in meta
    # Digest itself still built and persisted.
    assert payload["itemCount"] == 2
    assert (data_dir / "cache" / "today.json").is_file()
