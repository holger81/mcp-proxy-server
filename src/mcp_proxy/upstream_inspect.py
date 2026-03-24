"""Connect to an upstream MCP server and fetch tools / resources / prompts / capabilities."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Literal

import httpx
from mcp import types as mcp_types
from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, get_default_environment, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from mcp_proxy.models import UpstreamServer

InspectKind = Literal["tools", "resources", "prompts", "capabilities"]


class _SimplePostUnsupported(Exception):
    """One-shot JSON-RPC POST is not viable for this host; use the full streamable MCP client."""


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
        async with streamable_http_client(server.url, http_client=http_client) as transport:
            # mcp>=1.10 yields (read, write, get_session_id); older builds yield (read, write).
            read_stream, write_stream = transport[0], transport[1]
            yield read_stream, write_stream


async def _run_inspect_simple_jsonrpc_post(server: UpstreamServer, kind: InspectKind) -> dict:
    """One JSON-RPC POST per request (e.g. Home Assistant /api/mcp), multimodal mcpClient.js style.

    String UUID ids, application/json; no initialize before tools/list / resources/list / prompts/list.
    Capabilities uses a single initialize with protocolVersion 2024-11-05.
    """
    assert server.url
    url = str(server.url).strip()
    # Match StreamableHTTPTransport (mcp client): HA /api/mcp may return 406 if Accept is only JSON.
    hdrs = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        **(server.headers or {}),
    }

    def rpc(method: str, params: dict) -> dict:
        return {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": method,
            "params": params,
        }

    async def post_json(client: httpx.AsyncClient, payload: dict) -> dict | None:
        try:
            r = await client.post(url, json=payload, headers=hdrs)
        except httpx.RequestError as e:
            raise _SimplePostUnsupported(str(e) or type(e).__name__) from e
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            code = e.response.status_code if e.response is not None else 0
            if code in (401, 403):
                raise
            raise _SimplePostUnsupported(f"HTTP {code}") from e
        if r.status_code in (202, 204) or not (r.content or b"").strip():
            return None
        try:
            data = r.json()
        except ValueError as e:
            raise _SimplePostUnsupported("response is not JSON") from e
        if not isinstance(data, dict):
            raise _SimplePostUnsupported("response JSON is not an object")
        if data.get("error") is not None:
            err = data["error"]
            if isinstance(err, dict):
                msg = err.get("message", str(err))
            else:
                msg = str(err)
            raise _SimplePostUnsupported(msg)
        return data.get("result")

    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        if kind == "capabilities":
            init_result = await post_json(
                client,
                rpc(
                    "initialize",
                    {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "mcp-proxy-admin", "version": "0.1.0"},
                    },
                ),
            )
            if init_result is None:
                raise _SimplePostUnsupported("initialize returned an empty response")
            init_model = mcp_types.InitializeResult.model_validate(init_result)
            return {
                "kind": kind,
                "initialize": init_model.model_dump(mode="json", by_alias=True, exclude_none=True),
            }

        if kind == "tools":
            res = await post_json(client, rpc("tools/list", {}))
            if res is None:
                raise _SimplePostUnsupported("tools/list returned an empty response")
            ltr = mcp_types.ListToolsResult.model_validate(res)
            return {
                "kind": kind,
                "tools": [t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in ltr.tools],
            }
        if kind == "resources":
            res = await post_json(client, rpc("resources/list", {}))
            if res is None:
                raise _SimplePostUnsupported("resources/list returned an empty response")
            lr = mcp_types.ListResourcesResult.model_validate(res)
            return {
                "kind": kind,
                "resources": [
                    x.model_dump(mode="json", by_alias=True, exclude_none=True) for x in lr.resources
                ],
            }
        if kind == "prompts":
            res = await post_json(client, rpc("prompts/list", {}))
            if res is None:
                raise _SimplePostUnsupported("prompts/list returned an empty response")
            lp = mcp_types.ListPromptsResult.model_validate(res)
            return {
                "kind": kind,
                "prompts": [
                    x.model_dump(mode="json", by_alias=True, exclude_none=True) for x in lp.prompts
                ],
            }
    raise ValueError(f"unknown inspect kind: {kind}")


async def run_inspect(server: UpstreamServer, kind: InspectKind) -> dict:
    simple_post_exc: _SimplePostUnsupported | None = None
    if server.type == "http" and server.http_transport == "streamable-http":
        try:
            return await _run_inspect_simple_jsonrpc_post(server, kind)
        except _SimplePostUnsupported as e:
            simple_post_exc = e

    try:
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
    except Exception as e:
        if simple_post_exc is not None:
            raise RuntimeError(
                f"{upstream_error_detail(e)} (simple JSON-RPC POST first: {simple_post_exc})"
            ) from e
        raise


async def run_inspect_with_timeout(server: UpstreamServer, kind: InspectKind, timeout: float = 60.0) -> dict:
    try:
        return await asyncio.wait_for(run_inspect(server, kind), timeout=timeout)
    except asyncio.TimeoutError as e:
        raise TimeoutError(f"upstream {server.id!r} did not respond within {timeout}s") from e
