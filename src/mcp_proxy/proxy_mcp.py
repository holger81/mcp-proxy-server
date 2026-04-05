"""MCP proxy: only exposes searchToolsForDomain, searchTool, and callTool (domains group upstreams)."""

from __future__ import annotations

import json
import logging
from typing import Any

import anyio
from mcp import types as mcp_types
from mcp.client.session import ClientSession
from mcp.server import Server
from mcp.shared.exceptions import McpError

from mcp_proxy.config_store import ServerConfigStore
from mcp_proxy.domain_store import DomainStore
from mcp_proxy.models import UpstreamServer
from mcp_proxy.settings import Settings
from mcp_proxy.upstream_inspect import _upstream_streams

log = logging.getLogger(__name__)

_UPSTREAM_TIMEOUT_S = 120.0
_TRUNC_SUFFIX = " …[truncated]"


def _truncate_text(text: str, max_chars: int, suffix: str = _TRUNC_SUFFIX) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    keep = max(0, max_chars - len(suffix))
    return text[:keep] + suffix


def _llm_limits_excerpt(settings: Settings) -> dict[str, Any]:
    return {
        "tool_search_max_matches": settings.tool_search_max_matches,
        "tool_domain_default_limit": settings.tool_domain_default_limit,
        "tool_domain_max_limit": settings.tool_domain_max_limit,
        "tool_description_max_chars": settings.tool_description_max_chars,
        "tool_server_llm_context_max_chars": settings.tool_server_llm_context_max_chars,
        "tool_input_schema_max_chars": settings.tool_input_schema_max_chars,
        "call_tool_response_text_max_chars": settings.call_tool_response_text_max_chars,
        "tool_discovery_compact_json": settings.tool_discovery_compact_json,
        "instructions_max_chars": settings.instructions_max_chars,
    }


def _shape_tool_row_for_llm(row: dict[str, Any], settings: Settings) -> dict[str, Any]:
    out = dict(row)
    dmax = settings.tool_description_max_chars
    if dmax > 0:
        out["description"] = _truncate_text(str(out.get("description", "")), dmax)
    ctxmax = settings.tool_server_llm_context_max_chars
    if ctxmax > 0 and "serverLlmContext" in out:
        out["serverLlmContext"] = _truncate_text(str(out["serverLlmContext"]), ctxmax)
    smax = settings.tool_input_schema_max_chars
    if smax > 0:
        schema = out.get("inputSchema")
        raw = json.dumps(schema, default=str) if schema is not None else "{}"
        if len(raw) > smax:
            out["inputSchema"] = {
                "type": "object",
                "additionalProperties": True,
                "_proxySchemaTruncated": True,
                "_proxySchemaApproxChars": len(raw),
                "_proxyHint": (
                    f"Serialized inputSchema exceeded MCP_PROXY_TOOL_INPUT_SCHEMA_MAX_CHARS ({smax}); "
                    "raise that limit for the full JSON Schema."
                ),
            }
    return out


def _json_discovery(payload: Any, settings: Settings) -> str:
    if settings.tool_discovery_compact_json:
        return json.dumps(payload, default=str, separators=(",", ":"))
    return json.dumps(payload, indent=2, default=str)


def _coerce_bool_arg(key: str, raw: object) -> bool:
    if raw is None:
        return False
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        s = raw.strip().lower()
        if s in ("1", "true", "yes", "on"):
            return True
        if s in ("0", "false", "no", "off", ""):
            return False
        raise McpError(
            mcp_types.ErrorData(
                code=mcp_types.INVALID_PARAMS,
                message=f"{key!r} must be a boolean (got string {raw!r}).",
            )
        )
    if isinstance(raw, (int, float)):
        return bool(raw)
    raise McpError(
        mcp_types.ErrorData(
            code=mcp_types.INVALID_PARAMS,
            message=f"{key!r} must be a boolean.",
        )
    )


def _parse_domain_pagination(args: dict[str, Any], settings: Settings) -> tuple[int, int]:
    off_raw = args.get("offset", 0)
    try:
        offset = int(off_raw) if off_raw is not None else 0
    except (TypeError, ValueError):
        raise McpError(
            mcp_types.ErrorData(
                code=mcp_types.INVALID_PARAMS,
                message="'offset' must be a non-negative integer.",
            )
        )
    if offset < 0:
        raise McpError(
            mcp_types.ErrorData(
                code=mcp_types.INVALID_PARAMS,
                message="'offset' must be >= 0.",
            )
        )
    max_lim = settings.tool_domain_max_limit
    default_lim = min(settings.tool_domain_default_limit, max_lim)
    lim_raw = args.get("limit")
    if lim_raw is None:
        page_limit = default_lim
    else:
        try:
            page_limit = int(lim_raw)
        except (TypeError, ValueError):
            raise McpError(
                mcp_types.ErrorData(
                    code=mcp_types.INVALID_PARAMS,
                    message="'limit' must be a positive integer.",
                )
            )
        if page_limit < 1:
            raise McpError(
                mcp_types.ErrorData(
                    code=mcp_types.INVALID_PARAMS,
                    message="'limit' must be >= 1.",
                )
            )
    page_limit = min(page_limit, max_lim)
    return offset, page_limit


def _tool_row_matches_query(row: dict[str, Any], q_lower: str) -> bool:
    tn = str(row.get("toolName", "")).lower()
    desc = str(row.get("description", "")).lower()
    return q_lower in tn or q_lower in desc


def _rank_match_key(m: dict[str, Any], q_lower: str) -> tuple[int, str]:
    tn = str(m.get("toolName", "")).lower()
    if tn == q_lower:
        return (0, tn)
    if tn.startswith(q_lower):
        return (1, tn)
    return (2, tn)


def _truncate_call_tool_content(
    blocks: list[mcp_types.ContentBlock], max_chars: int
) -> list[mcp_types.ContentBlock]:
    if max_chars <= 0:
        return blocks
    out: list[mcp_types.ContentBlock] = []
    for b in blocks:
        if isinstance(b, mcp_types.TextContent):
            t = b.text
            if isinstance(t, str) and len(t) > max_chars:
                out.append(
                    mcp_types.TextContent(type="text", text=_truncate_text(t, max_chars))
                )
            else:
                out.append(b)
        else:
            out.append(b)
    return out


def _split_proxy_tool_name(name: str) -> tuple[str, str]:
    if "/" not in name:
        raise McpError(
            mcp_types.ErrorData(
                code=mcp_types.INVALID_PARAMS,
                message=(
                    f"Invalid toolName {name!r}: expected `<server-id>/<upstream-tool-name>` "
                    "as returned by searchToolsForDomain or searchTool."
                ),
            )
        )
    sid, tool = name.split("/", 1)
    if not sid or not tool:
        raise McpError(
            mcp_types.ErrorData(
                code=mcp_types.INVALID_PARAMS,
                message=f"Invalid toolName {name!r}: empty server id or tool name",
            )
        )
    return sid, tool


async def _list_upstream_tools(server: UpstreamServer) -> list[mcp_types.Tool]:
    with anyio.fail_after(_UPSTREAM_TIMEOUT_S):
        async with _upstream_streams(server) as (read_stream, write_stream):
            async with ClientSession(
                read_stream,
                write_stream,
                client_info=mcp_types.Implementation(name="mcp-proxy", version="0.1.0"),
            ) as session:
                await session.initialize()
                res = await session.list_tools()
                return list(res.tools)


def _tool_defs_for_server(server: UpstreamServer, tools: list[mcp_types.Tool]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    note = (server.llm_context or "").strip()
    for t in tools:
        composite = f"{server.id}/{t.name}"
        row: dict[str, Any] = {
            "toolName": composite,
            "description": (t.description or "").strip(),
            "domain": server.domain,
            "serverId": server.id,
            "inputSchema": t.inputSchema,
        }
        if note:
            row["serverLlmContext"] = note
        out.append(row)
    return out


async def _collect_all_tool_defs(store: ServerConfigStore, domain_id: str | None) -> list[dict[str, Any]]:
    combined: list[dict[str, Any]] = []
    for s in store.list_servers():
        if not s.enabled:
            continue
        if domain_id is not None and s.domain != domain_id:
            continue
        try:
            tools = await _list_upstream_tools(s)
            combined.extend(_tool_defs_for_server(s, tools))
        except TimeoutError:
            log.warning("collect tools: timeout for upstream %s", s.id)
        except Exception:
            log.exception("collect tools: skip upstream %s", s.id)
    return combined


def _domain_enum_schema(domain_ids: list[str], description: str) -> dict[str, Any]:
    if not domain_ids:
        domain_ids = ["default"]
    return {"type": "string", "enum": domain_ids, "description": description}


def _base_instructions() -> str:
    return (
        "You are connected through an MCP proxy. Individual upstream tools are NOT listed at the top level. "
        "As an LLM, you must use exactly the three tools below in sequence when you need a capability "
        "(e.g. smart home, network, or other MCP backends registered in the proxy).\n\n"
        "Workflow:\n"
        "1) Choose a domain id from the `domain` enum on searchToolsForDomain / searchTool (refreshed each "
        "tools/list). Domains group upstream servers (configured in the proxy admin UI).\n"
        "2) Discover tools: call searchToolsForDomain(domain, query) with a specific substring to limit results "
        "(name/description, case-insensitive); responses are paginated (offset/limit, hasMore). "
        "Only when you truly need the full catalog, set listAll=true and page through every tool in that domain "
        "(same pagination). "
        "Or use searchTool(query, domain optional) to search across domains.\n"
        "3) Read the JSON response: each entry includes toolName, description, domain, serverId, inputSchema, "
        "and optionally serverLlmContext (admin notes for that upstream). "
        "Use inputSchema to know required and optional parameters.\n"
        "4) Execute: call callTool with toolName exactly as returned (format `<server-id>/<upstream-tool-name>`) "
        "and arguments as a JSON object matching that schema.\n\n"
        "Do not invent tool names. Always obtain toolName from searchToolsForDomain or searchTool first. "
        "If unsure which domain applies, try searchTool with a broad query across all domains (omit domain), "
        "then narrow down."
    )


def full_instructions_for_store(store: ServerConfigStore, instructions_max_chars: int = 0) -> str:
    parts = [_base_instructions(), "", "## Upstream server notes (admin → LLM context)", ""]
    any_note = False
    for s in sorted(store.list_servers(), key=lambda x: x.id):
        note = (s.llm_context or "").strip()
        if not note:
            continue
        any_note = True
        title = f"{s.id} ({s.display_name})" if s.display_name else s.id
        parts.append(f"### {title}")
        parts.append(note)
        parts.append("")
    if not any_note:
        parts.append(
            "_No per-server notes yet. Configure \"LLM / instructions\" for each server in the admin UI._"
        )
        parts.append("")
    text = "\n".join(parts)
    return _truncate_text(text, instructions_max_chars) if instructions_max_chars > 0 else text


def build_meta_tool_list(domain_ids: list[str]) -> list[mcp_types.Tool]:
    dom = _domain_enum_schema(
        domain_ids,
        "Domain id (unique). Choose one; configure domains in the proxy admin UI.",
    )
    dom_opt = _domain_enum_schema(
        domain_ids,
        "Optional: restrict search to this domain id.",
    )
    return [
        mcp_types.Tool(
            name="searchToolsForDomain",
            description=(
                "For LLMs: discovery inside one domain. Prefer a non-empty `query` substring (tool name or "
                "description) so results stay small; each response is one page with pagination metadata (hasMore, "
                "offset, total). "
                "Set listAll=true only when you must enumerate every tool in the domain, then paginate with "
                "offset until hasMore is false. "
                "Then call callTool with toolName from tools[]."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "domain": dom,
                    "query": {
                        "type": "string",
                        "description": (
                            "Substring filter on tool name and description (case-insensitive). "
                            "Required unless listAll is true. Use specific terms to limit context."
                        ),
                    },
                    "listAll": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "If true, return all tools in the domain in pages (sorted by toolName). "
                            "Omit or empty `query`. Increase offset to read the full catalog."
                        ),
                    },
                    "offset": {
                        "type": "integer",
                        "minimum": 0,
                        "default": 0,
                        "description": "Pagination: skip this many tools after sort/filter.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "Pagination: max tools in this response (server caps). Omit for default.",
                    },
                },
                "required": ["domain"],
            },
        ),
        mcp_types.Tool(
            name="searchTool",
            description=(
                "For LLMs: discovery when you have a keyword (e.g. 'light', 'wifi') but not the exact tool. "
                "Returns JSON matches with toolName, domain, serverId, inputSchema, and optional serverLlmContext. "
                "Optional `domain` limits search to one domain. "
                "After picking a tool, call callTool with that toolName and arguments from inputSchema."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Substring to match (case-insensitive).",
                    },
                    "domain": dom_opt,
                },
                "required": ["query"],
            },
        ),
        mcp_types.Tool(
            name="callTool",
            description=(
                "For LLMs: execution step only. "
                "Pass toolName exactly as returned by searchToolsForDomain or searchTool "
                "(`<server-id>/<upstream-tool-name>`). "
                "Pass arguments as a JSON object; shape must match the tool's inputSchema from the search result."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "toolName": {
                        "type": "string",
                        "description": "Format: `<server-id>/<upstream-tool-name>`.",
                    },
                    "arguments": {
                        "type": "object",
                        "description": "JSON object of parameters for the upstream tool.",
                        "additionalProperties": True,
                    },
                },
                "required": ["toolName"],
            },
        ),
    ]


def get_llm_preview_snapshot(
    store: ServerConfigStore,
    domain_store: DomainStore,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Serializable view of what MCP clients receive for tools + instructions (admin preview)."""
    cfg = settings or Settings()
    ids = sorted({d.id for d in domain_store.list_records()})
    if not ids:
        ids = ["default"]
    tools = build_meta_tool_list(ids)
    tool_dicts = [t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in tools]
    return {
        "server": {
            "name": "mcp-proxy",
            "version": "0.1.0",
            "role": "Aggregates upstream MCP servers; only the three meta-tools are listed at session level.",
        },
        "instructions": full_instructions_for_store(store, cfg.instructions_max_chars),
        "tools": tool_dicts,
        "extras": {
            "upstream_tools": (
                "Hidden until searchToolsForDomain / searchTool; searchToolsForDomain uses query + pagination "
                "(or listAll + pagination). Entries may include serverLlmContext when configured per server."
            ),
            "protocol": (
                "Full initialize/capabilities exchange is handled by the MCP SDK; "
                "this preview focuses on instructions + listed tools."
            ),
            "llm_context_limits": _llm_limits_excerpt(cfg),
        },
    }


def build_proxy_mcp_server(
    store: ServerConfigStore,
    domain_store: DomainStore,
    settings: Settings,
) -> Server:
    server = Server(
        "mcp-proxy",
        version="0.1.0",
        instructions=full_instructions_for_store(store, settings.instructions_max_chars),
    )

    def _domain_ids() -> list[str]:
        ids = sorted({d.id for d in domain_store.list_records()})
        return ids if ids else ["default"]

    @server.list_tools()
    async def list_tools() -> list[mcp_types.Tool]:
        server.instructions = full_instructions_for_store(store, settings.instructions_max_chars)
        return build_meta_tool_list(_domain_ids())

    @server.call_tool()
    async def call_tool(
        name: str, arguments: dict | None
    ) -> list[mcp_types.ContentBlock]:
        args = arguments or {}

        if name == "searchToolsForDomain":
            dom = args.get("domain")
            if not isinstance(dom, str) or not dom.strip():
                raise McpError(
                    mcp_types.ErrorData(
                        code=mcp_types.INVALID_PARAMS,
                        message="Missing or invalid 'domain' (string).",
                    )
                )
            dom = dom.strip()
            if dom not in domain_store.id_set():
                raise McpError(
                    mcp_types.ErrorData(
                        code=mcp_types.INVALID_PARAMS,
                        message=f"Unknown domain {dom!r}. Known: {sorted(domain_store.id_set())}",
                    )
                )
            list_all = _coerce_bool_arg("listAll", args.get("listAll"))
            query_raw = args.get("query")
            query_ok = isinstance(query_raw, str) and bool(query_raw.strip())
            if list_all and query_ok:
                raise McpError(
                    mcp_types.ErrorData(
                        code=mcp_types.INVALID_PARAMS,
                        message=(
                            "Use either listAll=true (full catalog, paginated) or a non-empty query, not both. "
                            "Omit query when listing all tools."
                        ),
                    )
                )
            if not list_all and not query_ok:
                raise McpError(
                    mcp_types.ErrorData(
                        code=mcp_types.INVALID_PARAMS,
                        message=(
                            "Provide a non-empty query to search within the domain, or set listAll=true to "
                            "enumerate all tools with pagination (offset/limit, hasMore)."
                        ),
                    )
                )
            offset, page_limit = _parse_domain_pagination(args, settings)
            defs = await _collect_all_tool_defs(store, dom)
            if list_all:
                ordered = sorted(defs, key=lambda r: str(r.get("toolName", "")).lower())
                total = len(ordered)
                page = ordered[offset : offset + page_limit]
                mode = "listAll"
            else:
                q = str(query_raw).strip().lower()
                matches = [d for d in defs if _tool_row_matches_query(d, q)]
                matches.sort(key=lambda m: _rank_match_key(m, q))
                total = len(matches)
                page = matches[offset : offset + page_limit]
                mode = "filtered"
            shaped = [_shape_tool_row_for_llm(d, settings) for d in page]
            returned = len(shaped)
            payload: dict[str, Any] = {
                "mode": mode,
                "domain": dom,
                "tools": shaped,
                "pagination": {
                    "offset": offset,
                    "limit": page_limit,
                    "returned": returned,
                    "total": total,
                    "hasMore": offset + returned < total,
                },
            }
            return [
                mcp_types.TextContent(
                    type="text",
                    text=_json_discovery(payload, settings),
                )
            ]

        if name == "searchTool":
            query = args.get("query")
            if not isinstance(query, str) or not query.strip():
                raise McpError(
                    mcp_types.ErrorData(
                        code=mcp_types.INVALID_PARAMS,
                        message="Missing or invalid 'query' (non-empty string).",
                    )
                )
            q = query.strip().lower()
            dom_filter: str | None = None
            if "domain" in args and args["domain"] is not None:
                if not isinstance(args["domain"], str) or not args["domain"].strip():
                    raise McpError(
                        mcp_types.ErrorData(
                            code=mcp_types.INVALID_PARAMS,
                            message="If provided, 'domain' must be a non-empty string.",
                        )
                    )
                dom_filter = args["domain"].strip()
                if dom_filter not in domain_store.id_set():
                    raise McpError(
                        mcp_types.ErrorData(
                            code=mcp_types.INVALID_PARAMS,
                            message=f"Unknown domain {dom_filter!r}.",
                        )
                    )

            all_defs = await _collect_all_tool_defs(store, dom_filter)
            matches = [d for d in all_defs if _tool_row_matches_query(d, q)]
            matches.sort(key=lambda m: _rank_match_key(m, q))
            search_max = settings.tool_search_max_matches
            if search_max > 0:
                matches = matches[:search_max]
            shaped = [_shape_tool_row_for_llm(m, settings) for m in matches]
            return [
                mcp_types.TextContent(
                    type="text",
                    text=_json_discovery(shaped, settings),
                )
            ]

        if name == "callTool":
            tool_name = args.get("toolName")
            if not isinstance(tool_name, str) or not tool_name.strip():
                raise McpError(
                    mcp_types.ErrorData(
                        code=mcp_types.INVALID_PARAMS,
                        message="Missing or invalid 'toolName' (string).",
                    )
                )
            tool_args = args.get("arguments")
            if tool_args is not None and not isinstance(tool_args, dict):
                raise McpError(
                    mcp_types.ErrorData(
                        code=mcp_types.INVALID_PARAMS,
                        message="'arguments' must be a JSON object when provided.",
                    )
                )
            sid, orig = _split_proxy_tool_name(tool_name.strip())
            upstream = store.get(sid)
            if upstream is None or not upstream.enabled:
                raise McpError(
                    mcp_types.ErrorData(
                        code=mcp_types.INVALID_PARAMS,
                        message=f"Unknown or disabled upstream server {sid!r}",
                    )
                )
            try:
                with anyio.fail_after(_UPSTREAM_TIMEOUT_S):
                    async with _upstream_streams(upstream) as (read_stream, write_stream):
                        async with ClientSession(
                            read_stream,
                            write_stream,
                            client_info=mcp_types.Implementation(name="mcp-proxy", version="0.1.0"),
                        ) as session:
                            await session.initialize()
                            result = await session.call_tool(orig, tool_args)
            except McpError:
                raise
            except TimeoutError as e:
                raise McpError(
                    mcp_types.ErrorData(
                        code=mcp_types.INTERNAL_ERROR,
                        message=f"Upstream {sid!r} timed out",
                    )
                ) from e
            except Exception as e:
                log.exception("callTool failed for %s", tool_name)
                raise McpError(
                    mcp_types.ErrorData(
                        code=mcp_types.INTERNAL_ERROR,
                        message=str(e) or type(e).__name__,
                    )
                ) from e
            return _truncate_call_tool_content(
                list(result.content or []),
                settings.call_tool_response_text_max_chars,
            )

        raise McpError(
            mcp_types.ErrorData(
                code=mcp_types.METHOD_NOT_FOUND,
                message=f"Unknown tool {name!r}. Use searchToolsForDomain, searchTool, or callTool.",
            )
        )

    return server
