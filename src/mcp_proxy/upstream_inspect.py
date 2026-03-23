"""Connect to an upstream MCP server and fetch tools / resources / prompts / capabilities."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Literal

from mcp import types as mcp_types
from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, get_default_environment, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from mcp_proxy.models import UpstreamServer

InspectKind = Literal["tools", "resources", "prompts", "capabilities"]


def _is_wrapper_message(text: str) -> bool:
    t = text.lower()
    return (
        "taskgroup" in t
        or "exceptiongroup" in t
        or "sub-exception" in t
        or "unhandled errors" in t
    )


def upstream_error_detail(exc: BaseException, *, _seen: set[int] | None = None) -> str:
    """Flatten TaskGroup / ExceptionGroup so API clients see the real MCP/HTTP error."""
    if _seen is None:
        _seen = set()
    eid = id(exc)
    if eid in _seen:
        return str(exc).strip() or type(exc).__name__
    _seen.add(eid)

    if isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        nested: list[str] = []
        for sub in exc.exceptions[:8]:
            part = upstream_error_detail(sub, _seen=_seen)
            if part and not _is_wrapper_message(part):
                nested.append(part)
        if nested:
            return nested[0] if len(nested) == 1 else " | ".join(nested)
        return upstream_error_detail(exc.exceptions[0], _seen=_seen)

    top = str(exc).strip()
    for inner in (exc.__cause__, getattr(exc, "__context__", None)):
        if inner is None or id(inner) in _seen:
            continue
        deep = upstream_error_detail(inner, _seen=_seen)
        if _is_wrapper_message(top):
            return deep or top or type(exc).__name__
        if deep and deep not in top:
            return f"{top}: {deep}" if top else deep

    return top or type(exc).__name__


@asynccontextmanager
async def _upstream_streams(
    server: UpstreamServer,
) -> AsyncGenerator[tuple, None]:
    if server.type == "stdio":
        assert server.command and len(server.command) >= 1
        merged_env = {**get_default_environment(), **(server.env or {})}
        params = StdioServerParameters(
            command=server.command[0],
            args=list(server.command[1:]),
            env=merged_env,
            cwd=server.cwd,
        )
        async with stdio_client(params) as streams:
            yield streams
        return

    assert server.url and server.http_transport
    headers = server.headers or {}
    if server.http_transport == "sse":
        async with sse_client(server.url, headers=headers or None) as streams:
            yield streams
        return

    async with create_mcp_http_client(headers=headers) as http_client:
        async with streamable_http_client(server.url, http_client=http_client) as streams:
            yield streams


async def run_inspect(server: UpstreamServer, kind: InspectKind) -> dict:
    async with _upstream_streams(server) as (read_stream, write_stream):
        async with ClientSession(
            read_stream,
            write_stream,
            client_info=mcp_types.Implementation(name="mcp-proxy-admin", version="0.1.0"),
        ) as session:
            init = await session.initialize()
            if kind == "capabilities":
                return {
                    "kind": kind,
                    "initialize": init.model_dump(mode="json", by_alias=True, exclude_none=True),
                }
            if kind == "tools":
                result = await session.list_tools()
                return {
                    "kind": kind,
                    "tools": [t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in result.tools],
                }
            if kind == "resources":
                result = await session.list_resources()
                return {
                    "kind": kind,
                    "resources": [
                        r.model_dump(mode="json", by_alias=True, exclude_none=True) for r in result.resources
                    ],
                }
            if kind == "prompts":
                result = await session.list_prompts()
                return {
                    "kind": kind,
                    "prompts": [
                        p.model_dump(mode="json", by_alias=True, exclude_none=True) for p in result.prompts
                    ],
                }
            raise ValueError(f"unknown inspect kind: {kind}")


async def run_inspect_with_timeout(server: UpstreamServer, kind: InspectKind, timeout: float = 60.0) -> dict:
    try:
        return await asyncio.wait_for(run_inspect(server, kind), timeout=timeout)
    except asyncio.TimeoutError as e:
        raise TimeoutError(f"upstream {server.id!r} did not respond within {timeout}s") from e
