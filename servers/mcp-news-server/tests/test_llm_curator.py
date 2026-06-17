"""Tests for optional LLM digest curation."""

from __future__ import annotations

import json

import httpx

from mcp_news_server.llm_curator import (
    LlmCuratorConfig,
    _apply_selection,
    _parse_llm_json,
    maybe_curate_digest_payload,
)


def test_parse_llm_json_from_markdown_fence() -> None:
    raw = 'Here you go:\n```json\n{"briefing": "Hello", "selected": [{"index": 1}]}\n```'
    data = _parse_llm_json(raw)
    assert data["briefing"] == "Hello"


def test_apply_selection_reorders_items() -> None:
    items = [
        {"title": "A", "url": "https://a"},
        {"title": "B", "url": "https://b"},
        {"title": "C", "url": "https://c"},
    ]
    parsed = {
        "briefing": "Summary",
        "selected": [
            {"index": 3, "importance": "Local impact"},
            {"index": 1, "importance": "Big story"},
        ],
    }
    curated, notes = _apply_selection(items, parsed, top_n=5)
    assert [x["title"] for x in curated] == ["C", "A"]
    assert curated[0]["importance"] == "Local impact"
    assert len(notes) == 2


def test_plain_summary_strips_html_and_respects_limit() -> None:
    from mcp_news_server.llm_curator import _plain_summary

    assert _plain_summary("<p>Hello <b>world</b>.</p>", 0) == "Hello world."
    long = "A" * 100 + ". " + "B" * 100
    out = _plain_summary(long, 120)
    assert len(out) <= 121
    assert out.endswith(".") or out.endswith("…")


def test_maybe_curate_digest_payload_mocked() -> None:
    import asyncio

    payload = {
        "itemCount": 2,
        "items": [
            {"title": "Story one", "url": "https://1", "sourceName": "A"},
            {"title": "Story two", "url": "https://2", "sourceName": "B"},
        ],
        "errors": [],
        "meta": {"digest": "today", "updatedAt": "2026-01-01T00:00:00+00:00"},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat/completions")
        body = json.loads(request.content.decode())
        assert body["model"] == "test-model"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "briefing": "Two stories matter.",
                                    "selected": [
                                        {"index": 2, "importance": "More urgent"},
                                    ],
                                }
                            )
                        }
                    }
                ]
            },
        )

    cfg = LlmCuratorConfig(
        base_url="https://example.com/v1",
        api_key="secret",
        model="test-model",
        top_n=5,
        input_max=40,
        summary_max_chars=0,
        timeout_s=30.0,
        digests=frozenset({"today"}),
    )
    transport = httpx.MockTransport(handler)
    async def run() -> dict:
        async with httpx.AsyncClient(transport=transport) as client:
            return await maybe_curate_digest_payload(
                payload, digest="today", client=client, config=cfg
            )

    out = asyncio.run(run())

    assert out["briefing"] == "Two stories matter."
    assert out["itemCount"] == 1
    assert out["items"][0]["title"] == "Story two"
    assert out["meta"]["llmCurated"] is True


def test_maybe_curate_skips_when_not_configured() -> None:
    import asyncio

    payload = {"itemCount": 1, "items": [{"title": "X"}], "meta": {}}

    async def run() -> dict:
        async with httpx.AsyncClient() as client:
            return await maybe_curate_digest_payload(
                payload, digest="today", client=client, config=None
            )

    out = asyncio.run(run())
    assert out is payload
