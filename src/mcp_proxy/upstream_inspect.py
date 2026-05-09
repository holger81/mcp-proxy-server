"""Connect to an upstream MCP server and fetch tools / resources / prompts / capabilities."""

from __future__ import annotations

import asyncio
import io
import logging
import sys
import uuid
from contextlib import asynccontextmanager
from subprocess import PIPE
from typing import Any, AsyncGenerator, Literal

import anyio
import anyio.lowlevel
import httpx
from anyio.streams.text import TextReceiveStream
from mcp import types as mcp_types
from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import (
    PROCESS_TERMINATION_TIMEOUT,
    StdioServerParameters,
    get_default_environment,
    stdio_client,
)
from mcp.client.streamable_http import streamable_http_client
from mcp.os.posix.utilities import terminate_posix_process_tree
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp.shared.message import SessionMessage

from mcp_proxy.models import UpstreamServer
from mcp_proxy.stdio_node_inspect import stdio_effective_command

log = logging.getLogger(__name__)

InspectKind = Literal["tools", "resources", "prompts", "capabilities"]


class _SimplePostUnsupported(Exception):
    """One-shot JSON-RPC POST is not viable for this host; use the full streamable MCP client."""


def _is_method_not_found_text(msg: str) -> bool:
    m = (msg or "").lower()
    return "method not found" in m or m.strip() == "-32601"


def _is_method_not_found_exc(exc: BaseException) -> bool:
    return _is_method_not_found_text(upstream_error_detail(exc))


def _empty_resources_payload() -> dict:
    return {"kind": "resources", "resources": []}


def _empty_prompts_payload() -> dict:
    return {"kind": "prompts", "prompts": []}


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

    result = top or type(exc).__name__
    # anyio/asyncio: peer disconnected — almost always "stdio child exited before MCP initialize".
    if type(exc).__name__ == "BrokenResourceError":
        return (
            "BrokenResourceError (upstream stdio process exited before MCP initialize). "
            "Typical causes: missing/invalid startup config or env for that CLI, Node older than the "
            "package engines field, or the subprocess crashed — run the same command line and env inside "
            "the container to capture stderr."
        )
    return result


def format_upstream_stdio_error(
    exc: BaseException,
    stderr_text: str,
    *,
    stderr_max_chars: int = 12_000,
) -> str:
    """Combine flattened exception text with captured subprocess stderr (stdio upstreams)."""
    base = upstream_error_detail(exc)
    raw = stderr_text.strip()
    if not raw:
        return base
    if len(raw) > stderr_max_chars:
        raw = "… (stderr truncated) …\n" + raw[-stderr_max_chars:]
    return f"{base}\n\n--- stderr (upstream subprocess) ---\n{raw}"


@asynccontextmanager
async def _stdio_client_piped_stderr_capture(
    params: StdioServerParameters,
    stderr_sink: list[str],
) -> AsyncGenerator[tuple[Any, Any], None]:
    """Spawn stdio MCP like ``mcp.client.stdio``, but stderr=PIPE → capture + tty copy.

    ``anyio.open_process`` rejects file-like wrappers without ``fileno()``; Tee objects cannot satisfy
    that. Mirrors ``stdio_client`` stderr handling semantics on POSIX only.
    """
    read_stream_writer, read_stream = anyio.create_memory_object_stream(0)
    write_stream, write_stream_reader = anyio.create_memory_object_stream(0)

    command = params.command

    merged = (
        {**get_default_environment(), **params.env}
        if params.env is not None
        else get_default_environment()
    )

    try:
        process = await anyio.open_process(
            [command, *params.args],
            stdin=PIPE,
            stdout=PIPE,
            stderr=PIPE,
            env=merged,
            cwd=params.cwd,
            start_new_session=True,
        )
    except OSError:
        await read_stream.aclose()
        await write_stream.aclose()
        await read_stream_writer.aclose()
        await write_stream_reader.aclose()
        stderr_sink[:] = [""]
        raise

    async def stdout_reader() -> None:
        assert process.stdout, "Opened process is missing stdout"
        try:
            async with read_stream_writer:
                buffer = ""
                async for chunk in TextReceiveStream(
                    process.stdout,
                    encoding=params.encoding,
                    errors=params.encoding_error_handler,
                ):
                    lines = (buffer + chunk).split("\n")
                    buffer = lines.pop()
                    for line in lines:
                        try:
                            message = mcp_types.JSONRPCMessage.model_validate_json(line)
                        except Exception as exc:  # pragma: no cover
                            log.exception(
                                "Failed to parse JSONRPC message from upstream"
                            )
                            await read_stream_writer.send(exc)
                            continue
                        session_message = SessionMessage(message)
                        await read_stream_writer.send(session_message)
        except (
            anyio.ClosedResourceError,
            anyio.BrokenResourceError,
        ):  # pragma: no cover
            await anyio.lowlevel.checkpoint()

    async def stdin_writer() -> None:
        assert process.stdin, "Opened process is missing stdin"
        try:
            async with write_stream_reader:
                async for session_message in write_stream_reader:
                    json_rpc = session_message.message.model_dump_json(
                        by_alias=True, exclude_none=True
                    )
                    await process.stdin.send(
                        (json_rpc + "\n").encode(
                            encoding=params.encoding,
                            errors=params.encoding_error_handler,
                        )
                    )
        except (
            anyio.ClosedResourceError,
            anyio.BrokenResourceError,
        ):  # pragma: no cover
            await anyio.lowlevel.checkpoint()

    async def stderr_reader() -> None:
        capture = io.StringIO()
        assert process.stderr, "Opened process is missing stderr"
        try:
            async for chunk in TextReceiveStream(
                process.stderr,
                encoding=params.encoding,
                errors=params.encoding_error_handler,
            ):
                capture.write(chunk)
                sys.stderr.write(chunk)
                sys.stderr.flush()
        except (
            anyio.ClosedResourceError,
            anyio.BrokenResourceError,
        ):  # pragma: no cover
            await anyio.lowlevel.checkpoint()
        finally:
            stderr_sink[:] = [capture.getvalue()]

    async with anyio.create_task_group() as tg, process:
        tg.start_soon(stdout_reader)
        tg.start_soon(stdin_writer)
        tg.start_soon(stderr_reader)
        try:
            yield read_stream, write_stream
        finally:
            if process.stdin:  # pragma: no branch
                try:
                    await process.stdin.aclose()
                except Exception:  # pragma: no cover
                    pass
            try:
                with anyio.fail_after(PROCESS_TERMINATION_TIMEOUT):
                    await process.wait()
            except TimeoutError:
                await terminate_posix_process_tree(process)
            except ProcessLookupError:  # pragma: no cover
                pass
            await read_stream.aclose()
            await write_stream.aclose()
            await read_stream_writer.aclose()
            await write_stream_reader.aclose()


@asynccontextmanager
async def _upstream_streams(
    server: UpstreamServer,
    *,
    stdio_stderr_sink: list[str] | None = None,
) -> AsyncGenerator[tuple, None]:
    if server.type == "stdio":
        assert server.command and len(server.command) >= 1
        eff = stdio_effective_command(server)
        assert eff and len(eff) >= 1
        merged_env = {**get_default_environment(), **(server.env or {})}
        params = StdioServerParameters(
            command=eff[0],
            args=list(eff[1:]),
            env=merged_env,
            cwd=server.cwd,
        )
        # POSIX: always use piped stderr transport. Stock ``stdio_client`` only catches
        # ``ClosedResourceError`` on send; ``BrokenResourceError`` during teardown (common when
        # the child exits) becomes an ``ExceptionGroup`` and breaks tool discovery.
        # Windows keeps the MCP SDK stdio client (pipes + tee not wired for capture here).
        if sys.platform == "win32":
            if stdio_stderr_sink is not None:
                log.warning(
                    "stdio stderr capture is only supported on POSIX; stderr buffer left empty",
                )
                stdio_stderr_sink[:] = []
            async with stdio_client(params) as streams:
                yield streams
            return

        stderr_bucket = stdio_stderr_sink if stdio_stderr_sink is not None else []
        async with _stdio_client_piped_stderr_capture(params, stderr_bucket) as streams:
            yield streams
        return

    assert server.url and server.http_transport
    headers = server.headers or {}
    if server.http_transport == "sse":
        async with sse_client(server.url, headers=headers or None) as streams:
            yield streams
        return

    async with create_mcp_http_client(headers=headers) as http_client:
        async with streamable_http_client(
            server.url, http_client=http_client
        ) as transport:
            # mcp>=1.10 yields (read, write, get_session_id); older builds yield (read, write).
            read_stream, write_stream = transport[0], transport[1]
            yield read_stream, write_stream


async def _run_inspect_simple_jsonrpc_post(
    server: UpstreamServer, kind: InspectKind
) -> dict:
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
                code = err.get("code")
                msg = err.get("message", str(err))
                if code == -32601:
                    raise _SimplePostUnsupported("Method not found")
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
                "initialize": init_model.model_dump(
                    mode="json", by_alias=True, exclude_none=True
                ),
            }

        if kind == "tools":
            res = await post_json(client, rpc("tools/list", {}))
            if res is None:
                raise _SimplePostUnsupported("tools/list returned an empty response")
            ltr = mcp_types.ListToolsResult.model_validate(res)
            return {
                "kind": kind,
                "tools": [
                    t.model_dump(mode="json", by_alias=True, exclude_none=True)
                    for t in ltr.tools
                ],
            }
        if kind == "resources":
            try:
                res = await post_json(client, rpc("resources/list", {}))
            except _SimplePostUnsupported as e:
                if _is_method_not_found_text(str(e)):
                    return _empty_resources_payload()
                raise
            if res is None:
                raise _SimplePostUnsupported(
                    "resources/list returned an empty response"
                )
            lr = mcp_types.ListResourcesResult.model_validate(res)
            return {
                "kind": kind,
                "resources": [
                    x.model_dump(mode="json", by_alias=True, exclude_none=True)
                    for x in lr.resources
                ],
            }
        if kind == "prompts":
            try:
                res = await post_json(client, rpc("prompts/list", {}))
            except _SimplePostUnsupported as e:
                if _is_method_not_found_text(str(e)):
                    return _empty_prompts_payload()
                raise
            if res is None:
                raise _SimplePostUnsupported("prompts/list returned an empty response")
            lp = mcp_types.ListPromptsResult.model_validate(res)
            return {
                "kind": kind,
                "prompts": [
                    x.model_dump(mode="json", by_alias=True, exclude_none=True)
                    for x in lp.prompts
                ],
            }
    raise ValueError(f"unknown inspect kind: {kind}")


async def run_inspect(
    server: UpstreamServer,
    kind: InspectKind,
    *,
    stdio_stderr_holder: list[str] | None = None,
) -> dict:
    simple_post_exc: _SimplePostUnsupported | None = None
    if server.type == "http" and server.http_transport == "streamable-http":
        try:
            return await _run_inspect_simple_jsonrpc_post(server, kind)
        except _SimplePostUnsupported as e:
            simple_post_exc = e

    sink = stdio_stderr_holder if stdio_stderr_holder is not None else None
    try:
        async with _upstream_streams(server, stdio_stderr_sink=sink) as (
            read_stream,
            write_stream,
        ):
            async with ClientSession(
                read_stream,
                write_stream,
                client_info=mcp_types.Implementation(
                    name="mcp-proxy-admin", version="0.1.0"
                ),
            ) as session:
                init = await session.initialize()
                if kind == "capabilities":
                    return {
                        "kind": kind,
                        "initialize": init.model_dump(
                            mode="json", by_alias=True, exclude_none=True
                        ),
                    }
                if kind == "tools":
                    result = await session.list_tools()
                    return {
                        "kind": kind,
                        "tools": [
                            t.model_dump(mode="json", by_alias=True, exclude_none=True)
                            for t in result.tools
                        ],
                    }
                if kind == "resources":
                    try:
                        result = await session.list_resources()
                    except Exception as e:
                        if _is_method_not_found_exc(e):
                            return _empty_resources_payload()
                        raise
                    return {
                        "kind": kind,
                        "resources": [
                            r.model_dump(mode="json", by_alias=True, exclude_none=True)
                            for r in result.resources
                        ],
                    }
                if kind == "prompts":
                    try:
                        result = await session.list_prompts()
                    except Exception as e:
                        if _is_method_not_found_exc(e):
                            return _empty_prompts_payload()
                        raise
                    return {
                        "kind": kind,
                        "prompts": [
                            p.model_dump(mode="json", by_alias=True, exclude_none=True)
                            for p in result.prompts
                        ],
                    }
                raise ValueError(f"unknown inspect kind: {kind}")
    except Exception as e:
        stderr_txt = ""
        if stdio_stderr_holder:
            stderr_txt = str(stdio_stderr_holder[0] or "").strip()
        detail = format_upstream_stdio_error(e, stderr_txt)
        log.warning(
            "run_inspect failed (server_id=%r kind=%s transport=%s)",
            server.id,
            kind,
            server.type
            + ((":" + server.http_transport) if server.http_transport else ""),
            exc_info=e,
        )
        if simple_post_exc is not None:
            raise RuntimeError(
                f"{detail} (simple JSON-RPC POST first: {simple_post_exc})",
            ) from None
        raise RuntimeError(detail) from None


async def run_inspect_with_timeout(
    server: UpstreamServer,
    kind: InspectKind,
    timeout: float = 60.0,
    *,
    stdio_stderr_holder: list[str] | None = None,
) -> dict:
    try:
        return await asyncio.wait_for(
            run_inspect(server, kind, stdio_stderr_holder=stdio_stderr_holder),
            timeout=timeout,
        )
    except asyncio.TimeoutError as e:
        raise TimeoutError(
            f"upstream {server.id!r} did not respond within {timeout}s"
        ) from e
