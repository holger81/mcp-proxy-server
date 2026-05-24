"""Paginate large text callTool responses instead of hard truncation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from mcp import types as mcp_types
from mcp.shared.exceptions import McpError

from mcp_proxy.settings import Settings
from mcp_proxy.tool_response_cache import CachedToolResponse, ToolResponseCache

_TRUNC_SUFFIX = " …[truncated]"


@dataclass(frozen=True)
class ResponsePaginationRequest:
    cache_id: str | None
    offset: int
    limit: int | None


def _truncate_text(text: str, max_chars: int, suffix: str = _TRUNC_SUFFIX) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    keep = max(0, max_chars - len(suffix))
    return text[:keep] + suffix


def _json_payload(payload: Any, settings: Settings) -> str:
    if settings.tool_discovery_compact_json:
        return json.dumps(payload, default=str, separators=(",", ":"))
    return json.dumps(payload, indent=2, default=str)


def _join_text_blocks(blocks: list[mcp_types.ContentBlock]) -> str | None:
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, mcp_types.TextContent):
            parts.append(block.text if isinstance(block.text, str) else "")
        else:
            return None
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return "\n".join(parts)


def _truncate_blocks(
    blocks: list[mcp_types.ContentBlock], max_chars: int
) -> list[mcp_types.ContentBlock]:
    if max_chars <= 0:
        return blocks
    out: list[mcp_types.ContentBlock] = []
    for block in blocks:
        if isinstance(block, mcp_types.TextContent) and isinstance(block.text, str):
            if len(block.text) > max_chars:
                out.append(
                    mcp_types.TextContent(
                        type="text", text=_truncate_text(block.text, max_chars)
                    )
                )
            else:
                out.append(block)
        else:
            out.append(block)
    return out


def _page_size(settings: Settings) -> int:
    if settings.call_tool_response_page_chars > 0:
        return settings.call_tool_response_page_chars
    return settings.call_tool_response_text_max_chars


def parse_response_pagination(
    args: dict[str, Any], settings: Settings
) -> ResponsePaginationRequest:
    cache_raw = args.get("responseCacheId")
    cache_id: str | None = None
    if cache_raw is not None:
        if not isinstance(cache_raw, str) or not cache_raw.strip():
            raise McpError(
                mcp_types.ErrorData(
                    code=mcp_types.INVALID_PARAMS,
                    message="If provided, 'responseCacheId' must be a non-empty string.",
                )
            )
        cache_id = cache_raw.strip()

    offset = 0
    off_raw = args.get("responseOffset")
    if off_raw is not None:
        try:
            offset = int(off_raw)
        except (TypeError, ValueError) as e:
            raise McpError(
                mcp_types.ErrorData(
                    code=mcp_types.INVALID_PARAMS,
                    message="'responseOffset' must be an integer.",
                )
            ) from e
        if offset < 0:
            raise McpError(
                mcp_types.ErrorData(
                    code=mcp_types.INVALID_PARAMS,
                    message="'responseOffset' must be >= 0.",
                )
            )

    limit: int | None = None
    lim_raw = args.get("responseLimit")
    if lim_raw is not None:
        try:
            limit = int(lim_raw)
        except (TypeError, ValueError) as e:
            raise McpError(
                mcp_types.ErrorData(
                    code=mcp_types.INVALID_PARAMS,
                    message="'responseLimit' must be an integer.",
                )
            ) from e
        if limit < 1:
            raise McpError(
                mcp_types.ErrorData(
                    code=mcp_types.INVALID_PARAMS,
                    message="'responseLimit' must be >= 1.",
                )
            )

    page = _page_size(settings)
    if limit is not None and page > 0:
        limit = min(limit, page)

    return ResponsePaginationRequest(cache_id=cache_id, offset=offset, limit=limit)


def _build_page_payload(
    *,
    text_slice: str,
    offset: int,
    limit: int,
    total: int,
    cache_id: str,
    tool_name: str,
) -> dict[str, Any]:
    returned = len(text_slice)
    return {
        "text": text_slice,
        "pagination": {
            "offset": offset,
            "limit": limit,
            "returnedChars": returned,
            "totalChars": total,
            "hasMore": offset + returned < total,
            "responseCacheId": cache_id,
            "toolName": tool_name,
        },
        "hint": (
            "Large tool response split across pages. For the next slice, call callTool with the same "
            "toolName, this responseCacheId, and responseOffset set to offset + returnedChars "
            "(upstream arguments are not re-run)."
        ),
    }


def paginate_from_cache(
    entry: CachedToolResponse,
    *,
    settings: Settings,
    cache_id: str,
    offset: int,
    limit: int | None,
    tool_name: str,
) -> list[mcp_types.ContentBlock]:
    page = _page_size(settings)
    if page <= 0:
        return [mcp_types.TextContent(type="text", text=entry.text)]

    if tool_name and entry.tool_name and tool_name != entry.tool_name:
        raise McpError(
            mcp_types.ErrorData(
                code=mcp_types.INVALID_PARAMS,
                message=(
                    f"responseCacheId was created for tool {entry.tool_name!r}, "
                    f"not {tool_name!r}."
                ),
            )
        )

    total = len(entry.text)
    if offset >= total:
        raise McpError(
            mcp_types.ErrorData(
                code=mcp_types.INVALID_PARAMS,
                message=(
                    f"responseOffset {offset} is past end of cached response ({total} chars)."
                ),
            )
        )

    eff_limit = min(limit or page, page)
    text_slice = entry.text[offset : offset + eff_limit]
    payload = _build_page_payload(
        text_slice=text_slice,
        offset=offset,
        limit=eff_limit,
        total=total,
        cache_id=cache_id,
        tool_name=entry.tool_name,
    )
    return [mcp_types.TextContent(type="text", text=_json_payload(payload, settings))]


def paginate_call_tool_response(
    blocks: list[mcp_types.ContentBlock],
    *,
    settings: Settings,
    cache: ToolResponseCache,
    tool_name: str,
    pagination: ResponsePaginationRequest,
) -> list[mcp_types.ContentBlock]:
    page = _page_size(settings)
    if page <= 0:
        return _truncate_blocks(blocks, settings.call_tool_response_text_max_chars)

    if pagination.cache_id:
        entry = cache.get(pagination.cache_id)
        if entry is None:
            raise McpError(
                mcp_types.ErrorData(
                    code=mcp_types.INVALID_PARAMS,
                    message=(
                        "responseCacheId is missing or expired. Re-run callTool without "
                        "responseCacheId/responseOffset to fetch a fresh response."
                    ),
                )
            )
        return paginate_from_cache(
            entry,
            settings=settings,
            cache_id=pagination.cache_id,
            offset=pagination.offset,
            limit=pagination.limit,
            tool_name=tool_name,
        )

    text = _join_text_blocks(blocks)
    if text is None:
        return blocks

    if len(text) <= page:
        hard = settings.call_tool_response_text_max_chars
        if hard > 0 and len(text) > hard:
            return _truncate_blocks(blocks, hard)
        return blocks

    cache_id = cache.put(tool_name, text)
    eff_limit = min(pagination.limit or page, page)
    text_slice = text[:eff_limit]
    payload = _build_page_payload(
        text_slice=text_slice,
        offset=0,
        limit=eff_limit,
        total=len(text),
        cache_id=cache_id,
        tool_name=tool_name,
    )
    return [mcp_types.TextContent(type="text", text=_json_payload(payload, settings))]
