"""PR 3.3 (PLAN.md Phase 3): tool responses carry error classes, not details.

On main, per-feed/ingest errors echoed raw exception strings (internal URLs,
upstream stack text) back through MCP. They now return short stable classes
("http_403", "connect_failed", "timeout", ...) and full details go to the
server log; meta.llmError is summarized the same way.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import respx

from mcp_news_server.fetchers import gather_rss_for_feeds
from mcp_news_server.llm_curator import LlmCuratorConfig, maybe_curate_digest_payload
from mcp_news_server.models import FeedEntry

CFG = LlmCuratorConfig(
    base_url="https://llm.test/v1",
    api_key=None,
    model="test-model",
    top_n=5,
    input_max=20,
    summary_max_chars=1200,
    timeout_s=30.0,
    digests=frozenset({"today"}),
)


def _gather_one_feed(side_effect=None, response=None) -> dict:
    with respx.mock:
        respx.get("https://broken.test/rss.xml").mock(
            side_effect=side_effect, return_value=response
        )

        async def go():
            errors: list[dict] = []
            async with httpx.AsyncClient() as client:
                await gather_rss_for_feeds(
                    client,
                    [FeedEntry(url="https://broken.test/rss.xml", label="B")],
                    max_per=10,
                    errors=errors,
                )
            return errors

        errs = asyncio.run(go())
    assert len(errs) == 1
    return errs[0]


def test_http_status_error_is_class_only():
    err = _gather_one_feed(response=httpx.Response(403, text="nope secret detail"))
    assert err["error"] == "http_403"


def test_connect_error_details_stay_out_of_band():
    err = _gather_one_feed(
        side_effect=httpx.ConnectError("dial tcp 10.0.0.7:5432 refused internal-host")
    )
    assert err["error"] == "connect_failed"
    assert "10.0.0.7" not in json.dumps(err)


def test_timeout_class():
    err = _gather_one_feed(side_effect=httpx.ReadTimeout("read timed out internally"))
    assert err["error"] == "timeout"


def test_oversized_body_class():
    err = _gather_one_feed(response=httpx.Response(200, content=b"x" * 6_000_000))
    assert err["error"] == "response_too_large"


def _curate_with_llm(response: httpx.Response) -> dict:
    with respx.mock:
        respx.post("https://llm.test/v1/chat/completions").mock(
            return_value=response
        )
        payload = {
            "items": [{"title": "Story", "sourceName": "W", "summary": ""}],
            "itemCount": 1,
            "meta": {"updatedAt": "x"},
        }

        async def go():
            async with httpx.AsyncClient() as client:
                return await maybe_curate_digest_payload(
                    payload, digest="today", client=client, config=CFG
                )

        return asyncio.run(go())


def test_llm_error_http_class():
    out = _curate_with_llm(httpx.Response(500, text="upstream stacktrace details"))
    assert out["meta"]["llmError"] == "http_500"
    assert "stacktrace" not in out["meta"]["llmError"]


def test_llm_error_invalid_response_class():
    out = _curate_with_llm(httpx.Response(200, json={"choices": []}))
    assert out["meta"]["llmError"] == "invalid_llm_response"
