"""Aggregating MCP server: exposes enabled upstream tools as `<server-id>/<tool-name>`."""

from __future__ import annotations

import logging

import anyio
from mcp import types as mcp_types
from mcp.client.session import ClientSession
from mcp.server import Server
from mcp.shared.exceptions import McpError

from mcp_proxy.config_store import ServerConfigStore
from mcp_proxy.upstream_inspect import _upstream_streams

log = logging.getLogger(__name__)

_UPSTREAM_TIMEOUT_S = 120.0


def _split_proxy_tool_name(name: str) -> tuple[str, str]:
    if "/" not in name:
        raise McpError(
            mcp_types.ErrorData(
                code=mcp_types.INVALID_PARAMS,
                message=(
                    f"Invalid tool name {name!r}: expected `<server-id>/<upstream-tool-name>` "
                    "(server id is the slug from the admin UI)."
                ),
            )
        )
    sid, tool = name.split("/", 1)
    if not sid or not tool:
        raise McpError(
            mcp_types.ErrorData(
                code=mcp_types.INVALID_PARAMS,
                message=f"Invalid tool name {name!r}: empty server id or tool name",
            )
        )
    return sid, tool


def build_proxy_mcp_server(store: ServerConfigStore) -> Server:
    server = Server(
        "mcp-proxy",
        version="0.1.0",
        instructions=(
            "This server aggregates MCP tools from upstreams registered in the proxy admin UI. "
            "Each tool is named `<server-id>/<original-tool-name>`."
        ),
    )

    @server.list_tools()
    async def list_tools() -> list[mcp_types.Tool]:
        out: list[mcp_types.Tool] = []
        for s in store.list_servers():
            if not s.enabled:
                continue
            try:
                with anyio.fail_after(_UPSTREAM_TIMEOUT_S):
                    async with _upstream_streams(s) as (read_stream, write_stream):
                        async with ClientSession(
                            read_stream,
                            write_stream,
                            client_info=mcp_types.Implementation(name="mcp-proxy", version="0.1.0"),
                        ) as session:
                            await session.initialize()
                            res = await session.list_tools()
                            for t in res.tools:
                                composite = f"{s.id}/{t.name}"
                                desc = (t.description or "").strip()
                                suffix = f"[upstream: {s.id}]"
                                if suffix not in desc:
                                    desc = f"{desc} {suffix}".strip() if desc else suffix
                                out.append(
                                    mcp_types.Tool(
                                        name=composite,
                                        description=desc,
                                        inputSchema=t.inputSchema,
                                    )
                                )
            except TimeoutError:
                log.warning("list_tools: timeout for upstream %s", s.id)
            except Exception:
                log.exception("list_tools: skip upstream %s", s.id)
        return out

    @server.call_tool()
    async def call_tool(
        name: str, arguments: dict | None
    ) -> list[mcp_types.ContentBlock]:
        sid, orig = _split_proxy_tool_name(name)
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
                        result = await session.call_tool(orig, arguments)
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
            log.exception("call_tool failed for %s", name)
            raise McpError(
                mcp_types.ErrorData(
                    code=mcp_types.INTERNAL_ERROR,
                    message=str(e) or type(e).__name__,
                )
            ) from e
        return list(result.content or [])

    return server
