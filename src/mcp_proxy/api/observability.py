"""Admin-only diagnostics: buffered logs and MCP LLM-facing preview."""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

from mcp_proxy.log_buffer import get_ring_handler
from mcp_proxy.proxy_mcp import get_llm_preview_snapshot

router = APIRouter(tags=["observability"])


@router.get("/logs")
async def get_logs(
    limit: int = Query(
        default=500, ge=1, le=2000, description="Max lines from the end of the buffer"
    ),
) -> dict[str, list[str]]:
    return {"lines": get_ring_handler().get_lines(limit)}


@router.get("/mcp-llm-preview")
async def mcp_llm_preview(request: Request) -> dict:
    store = request.app.state.server_store
    domain_store = request.app.state.domain_store
    return await get_llm_preview_snapshot(
        store,
        domain_store,
        request.app.state.settings,
        stats_store=request.app.state.tool_call_stats_store,
    )


@router.get("/mcp-live")
async def mcp_live(
    request: Request,
    active_within_ms: int = Query(
        default=90_000,
        ge=1_000,
        le=60 * 60 * 1000,
        description="Consider a client connected if seen within this window (ms).",
    ),
) -> dict:
    tracker = request.app.state.live_mcp_tracker
    return await tracker.snapshot(active_within_ms=active_within_ms)
