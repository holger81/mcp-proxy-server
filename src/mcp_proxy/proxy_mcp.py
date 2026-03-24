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
from mcp_proxy.upstream_inspect import _upstream_streams

log = logging.getLogger(__name__)

_UPSTREAM_TIMEOUT_S = 120.0


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
        "2) Discover tools: call searchToolsForDomain(domain) to list all tools in that domain, OR "
        "searchTool(query, domain optional) to find tools by keyword in name/description.\n"
        "3) Read the JSON response: each entry includes toolName, description, domain, serverId, inputSchema, "
        "and optionally serverLlmContext (admin notes for that upstream). "
        "Use inputSchema to know required and optional parameters.\n"
        "4) Execute: call callTool with toolName exactly as returned (format `<server-id>/<upstream-tool-name>`) "
        "and arguments as a JSON object matching that schema.\n\n"
        "Do not invent tool names. Always obtain toolName from searchToolsForDomain or searchTool first. "
        "If unsure which domain applies, try searchTool with a broad query across all domains (omit domain), "
        "then narrow down."
    )


def full_instructions_for_store(store: ServerConfigStore) -> str:
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
    return "\n".join(parts)


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
                "For LLMs: first discovery step when you know the domain. "
                "Returns JSON listing toolName, description, inputSchema per upstream tool in that domain. "
                "Domains group MCP servers (e.g. smart home vs network). "
                "Use the enum for `domain`; then use callTool with a toolName from this list."
            ),
            inputSchema={
                "type": "object",
                "properties": {"domain": dom},
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
) -> dict[str, Any]:
    """Serializable view of what MCP clients receive for tools + instructions (admin preview)."""
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
        "instructions": full_instructions_for_store(store),
        "tools": tool_dicts,
        "extras": {
            "upstream_tools": (
                "Hidden until searchToolsForDomain / searchTool; JSON entries may include serverLlmContext "
                "when configured per server."
            ),
            "protocol": (
                "Full initialize/capabilities exchange is handled by the MCP SDK; "
                "this preview focuses on instructions + listed tools."
            ),
        },
    }


def build_proxy_mcp_server(store: ServerConfigStore, domain_store: DomainStore) -> Server:
    server = Server(
        "mcp-proxy",
        version="0.1.0",
        instructions=full_instructions_for_store(store),
    )

    def _domain_ids() -> list[str]:
        ids = sorted({d.id for d in domain_store.list_records()})
        return ids if ids else ["default"]

    @server.list_tools()
    async def list_tools() -> list[mcp_types.Tool]:
        server.instructions = full_instructions_for_store(store)
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
            defs = await _collect_all_tool_defs(store, dom)
            return [
                mcp_types.TextContent(
                    type="text",
                    text=json.dumps(defs, indent=2, default=str),
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
            matches: list[dict[str, Any]] = []
            for d in all_defs:
                tn = str(d.get("toolName", "")).lower()
                desc = str(d.get("description", "")).lower()
                if q in tn or q in desc:
                    matches.append(d)

            def _rank(m: dict[str, Any]) -> tuple[int, str]:
                tn = str(m.get("toolName", "")).lower()
                if tn == q:
                    return (0, tn)
                if tn.startswith(q):
                    return (1, tn)
                return (2, tn)

            matches.sort(key=_rank)
            matches = matches[:25]
            return [
                mcp_types.TextContent(
                    type="text",
                    text=json.dumps(matches, indent=2, default=str),
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
            return list(result.content or [])

        raise McpError(
            mcp_types.ErrorData(
                code=mcp_types.METHOD_NOT_FOUND,
                message=f"Unknown tool {name!r}. Use searchToolsForDomain, searchTool, or callTool.",
            )
        )

    return server
