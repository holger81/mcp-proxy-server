"""Paginate large text callTool responses instead of hard truncation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from mcp import types as mcp_types
from mcp.shared.exceptions import McpError

from mcp_proxy.settings import Settings
from mcp_proxy.tool_names import decode_proxy_tool_name
from mcp_proxy.tool_response_cache import CachedToolResponse, ToolResponseCache

_TRUNC_SUFFIX = " …[truncated]"

# Proxy-only callTool / composite-tool fields (stripped before upstream MCP invoke).
PAGINATION_ARG_KEYS: frozenset[str] = frozenset(
    {"responseCacheId", "responseOffset", "responseLimit"}
)

# PR 5.5: collision-proof namespaced spelling. Always reserved and stripped;
# the bare legacy names are accepted for one more release but skipped when the
# upstream tool declares a parameter of the same name.
_PROXY_PARAM_PREFIX = "_proxy."


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


def peel_pagination_params(
    raw: dict[str, Any],
    *,
    protected: frozenset[str] = frozenset(),
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Remove pagination keys from a flat argument object.

    ``_proxy.responseOffset``-style names are always proxy-owned. Bare legacy
    names are stripped too — except the ones in ``protected`` (declared by the
    upstream tool's inputSchema), which stay in ``rest`` as upstream args
    (PR 5.5). The returned dict always uses the bare canonical keys; the
    namespaced spelling wins over a legacy duplicate.
    """
    rest = dict(raw)
    pag: dict[str, Any] = {}
    for key in PAGINATION_ARG_KEYS:
        namespaced = _PROXY_PARAM_PREFIX + key
        if namespaced in rest:
            pag[key] = rest.pop(namespaced)
    for key in PAGINATION_ARG_KEYS:
        if key not in rest or key in protected:
            continue
        value = rest.pop(key)  # consumed either way; namespaced wins the value
        pag.setdefault(key, value)
    return rest, pag


def wrap_hot_tool_as_call_tool(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    declared_params: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Promote pagination fields from composite-tool args to callTool top level."""
    clean, pag = peel_pagination_params(arguments, protected=declared_params)
    out: dict[str, Any] = {"toolName": tool_name, "arguments": clean}
    out.update(pag)
    return out


def parse_call_tool_pagination(
    call_tool_args: dict[str, Any],
    settings: Settings,
    *,
    declared_params: frozenset[str] = frozenset(),
) -> tuple[dict[str, Any] | None, ResponsePaginationRequest]:
    """Read pagination from callTool args; strip it from upstream ``arguments``.

    Precedence (PR 5.5): top-level namespaced > top-level legacy > nested
    (inside ``arguments``) namespaced > nested legacy. ``declared_params``
    (the upstream inputSchema properties, when known) keep same-named upstream
    parameters out of the legacy peel.
    """
    merged_pag: dict[str, Any] = {}
    tool_args = call_tool_args.get("arguments")
    clean_upstream: dict[str, Any] | None = None
    if isinstance(tool_args, dict):
        clean_upstream, nested = peel_pagination_params(
            tool_args, protected=declared_params
        )
        merged_pag.update(nested)
    for key in PAGINATION_ARG_KEYS:
        if key in call_tool_args:
            merged_pag[key] = call_tool_args[key]
        namespaced = _PROXY_PARAM_PREFIX + key
        if namespaced in call_tool_args:
            merged_pag[key] = call_tool_args[namespaced]
    return clean_upstream, parse_response_pagination(merged_pag, settings)


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
            "Large tool response split across pages. For the next slice, call the same tool again "
            "(the composite toolName or callTool) with this responseCacheId and responseOffset set "
            "to offset + returnedChars. Pagination fields are proxy-only and are not sent upstream; "
            "if the upstream tool declares one of its own, use the namespaced spelling "
            "(_proxy.responseCacheId, _proxy.responseOffset, _proxy.responseLimit)."
        ),
    }


def _same_tool_name(a: str, b: str) -> bool:
    """True when both spellings resolve to the same tool across naming schemes.

    Cached pages may be created under the canonical name (``srv__tool``) and
    replayed via the legacy ``srv/tool`` spelling (PR 5.5); a raw string
    comparison wrongly rejected those.
    """
    if a == b:
        return True
    try:
        return decode_proxy_tool_name(a) == decode_proxy_tool_name(b)
    except ValueError:
        return False


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

    if tool_name and entry.tool_name and not _same_tool_name(tool_name, entry.tool_name):
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
        # PR 5.5: mixed content (text alongside image/audio/resource blocks)
        # bypassed every size limit. The non-text payload cannot be paged, so
        # cap the text blocks via truncation instead.
        hard = settings.call_tool_response_text_max_chars
        return _truncate_blocks(blocks, hard if hard > 0 else page)

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


def apply_upstream_error_semantics(
    result: mcp_types.CallToolResult,
    blocks: list[Any],
    *,
    settings: Settings,
) -> list[Any] | mcp_types.CallToolResult:
    """PR 5.2a: surface upstream ``isError`` / ``structuredContent`` when enabled.

    Off (default this release, D4 warn-first): today's behavior — content
    blocks only; an upstream tool error arrives as normal text and the SDK
    marks the result ``isError=False``.

    On: return a ``CallToolResult`` so the SDK passes ``isError`` through
    verbatim (tool errors stay tool errors) and ``structuredContent`` is
    forwarded untouched. ``blocks`` is the *paginated* text content; the
    pagination layer only ever rewrites text blocks, so structured content
    is unaffected by paging.
    """
    if not settings.propagate_tool_errors:
        return blocks
    if not result.isError and result.structuredContent is None:
        return blocks
    return mcp_types.CallToolResult(
        content=blocks,
        structuredContent=result.structuredContent,
        isError=bool(result.isError),
    )
