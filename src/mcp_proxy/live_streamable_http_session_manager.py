"""Bind MCP session id into a ContextVar for the lifetime of ``Server.run()``.

Streamable HTTP runs ``app.run()`` in a **background task**. Tool handlers execute
there, not in the ASGI worker task that served the HTTP request, so ContextVars
set by Starlette middleware never reach ``callTool``. We set the session id around
``app.run()`` instead (matches ``http_transport.mcp_session_id``).

Implementation is copied from ``mcp.server.streamable_http_manager`` (same control
flow) with a small wrapper around ``await self.app.run(...)``.
"""

from __future__ import annotations

import logging
from http import HTTPStatus
from uuid import uuid4

import anyio
from anyio.abc import TaskStatus
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import Receive, Scope, Send

from mcp.server.streamable_http import (
    MCP_SESSION_ID_HEADER,
    StreamableHTTPServerTransport,
)
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import INVALID_REQUEST, ErrorData, JSONRPCError

from mcp_proxy.live_mcp_tracker import current_mcp_session_id

logger = logging.getLogger(__name__)


class LiveBindingStreamableHTTPSessionManager(StreamableHTTPSessionManager):
    async def _run_mcp_server(
        self,
        http_transport: StreamableHTTPServerTransport,
        read_stream,
        write_stream,
        *,
        stateless: bool,
    ) -> None:
        sid = http_transport.mcp_session_id
        tok = current_mcp_session_id.set(sid) if sid else None
        try:
            await self.app.run(
                read_stream,
                write_stream,
                self.app.create_initialization_options(),
                stateless=stateless,
            )
        finally:
            if tok is not None:
                current_mcp_session_id.reset(tok)

    async def _handle_stateless_request(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        logger.debug("Stateless mode: Creating new transport for this request")
        http_transport = StreamableHTTPServerTransport(
            mcp_session_id=None,
            is_json_response_enabled=self.json_response,
            event_store=None,
            security_settings=self.security_settings,
        )

        async def run_stateless_server(
            *, task_status: TaskStatus[None] = anyio.TASK_STATUS_IGNORED
        ):
            async with http_transport.connect() as streams:
                read_stream, write_stream = streams
                task_status.started()
                try:
                    await self._run_mcp_server(
                        http_transport,
                        read_stream,
                        write_stream,
                        stateless=True,
                    )
                except Exception:  # pragma: no cover
                    logger.exception("Stateless session crashed")

        assert self._task_group is not None
        await self._task_group.start(run_stateless_server)

        await http_transport.handle_request(scope, receive, send)

        await http_transport.terminate()

    async def _handle_stateful_request(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        request = Request(scope, receive)
        request_mcp_session_id = request.headers.get(MCP_SESSION_ID_HEADER)

        if (
            request_mcp_session_id is not None
            and request_mcp_session_id in self._server_instances
        ):  # pragma: no cover
            transport = self._server_instances[request_mcp_session_id]
            logger.debug("Session already exists, handling request directly")
            if (
                transport.idle_scope is not None
                and self.session_idle_timeout is not None
            ):
                transport.idle_scope.deadline = (
                    anyio.current_time() + self.session_idle_timeout
                )
            await transport.handle_request(scope, receive, send)
            return

        if request_mcp_session_id is None:
            logger.debug("Creating new transport")
            async with self._session_creation_lock:
                new_session_id = uuid4().hex
                http_transport = StreamableHTTPServerTransport(
                    mcp_session_id=new_session_id,
                    is_json_response_enabled=self.json_response,
                    event_store=self.event_store,
                    security_settings=self.security_settings,
                    retry_interval=self.retry_interval,
                )

                assert http_transport.mcp_session_id is not None
                self._server_instances[http_transport.mcp_session_id] = http_transport
                logger.info("Created new transport with session ID: %s", new_session_id)

                async def run_server(
                    *, task_status: TaskStatus[None] = anyio.TASK_STATUS_IGNORED
                ) -> None:
                    async with http_transport.connect() as streams:
                        read_stream, write_stream = streams
                        task_status.started()
                        try:
                            idle_scope = anyio.CancelScope()
                            if self.session_idle_timeout is not None:
                                idle_scope.deadline = (
                                    anyio.current_time() + self.session_idle_timeout
                                )
                                http_transport.idle_scope = idle_scope

                            with idle_scope:
                                await self._run_mcp_server(
                                    http_transport,
                                    read_stream,
                                    write_stream,
                                    stateless=False,
                                )

                            if idle_scope.cancelled_caught:
                                assert http_transport.mcp_session_id is not None
                                logger.info(
                                    "Session %s idle timeout",
                                    http_transport.mcp_session_id,
                                )
                                self._server_instances.pop(
                                    http_transport.mcp_session_id, None
                                )
                                await http_transport.terminate()
                        except Exception:
                            logger.exception(
                                "Session %s crashed", http_transport.mcp_session_id
                            )
                        finally:
                            if (
                                http_transport.mcp_session_id
                                and http_transport.mcp_session_id
                                in self._server_instances
                                and not http_transport.is_terminated
                            ):
                                logger.info(
                                    "Cleaning up crashed session %s from active instances.",
                                    http_transport.mcp_session_id,
                                )
                                del self._server_instances[
                                    http_transport.mcp_session_id
                                ]

                assert self._task_group is not None
                await self._task_group.start(run_server)

                await http_transport.handle_request(scope, receive, send)
        else:
            error_response = JSONRPCError(
                jsonrpc="2.0",
                id="server-error",
                error=ErrorData(
                    code=INVALID_REQUEST,
                    message="Session not found",
                ),
            )
            response = Response(
                content=error_response.model_dump_json(
                    by_alias=True, exclude_none=True
                ),
                status_code=HTTPStatus.NOT_FOUND,
                media_type="application/json",
            )
            await response(scope, receive, send)
