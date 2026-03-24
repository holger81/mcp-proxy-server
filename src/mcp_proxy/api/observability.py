"""Admin-only diagnostics: buffered logs and MCP LLM-facing preview."""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

from mcp_proxy.log_buffer import get_ring_handler
from mcp_proxy.proxy_mcp import get_llm_preview_snapshot

router = APIRouter(tags=["observability"])


@router.get("/logs")
async def get_logs(
    limit: int = Query(default=500, ge=1, le=2000, description="Max lines from the end of the buffer"),
) -> dict[str, list[str]]:
    return {"lines": get_ring_handler().get_lines(limit)}


@router.get("/mcp-llm-preview")
async def mcp_llm_preview(request: Request) -> dict:
    store = request.app.state.server_store
    domain_store = request.app.state.domain_store
    return get_llm_preview_snapshot(store, domain_store)
