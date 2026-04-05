from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.types import Receive, Scope, Send

from mcp_proxy.api.routes import router as api_router
from mcp_proxy.client_store import ClientTokenStore
from mcp_proxy.log_buffer import attach_ring_logging
from mcp_proxy.config_store import ServerConfigStore
from mcp_proxy.domain_store import DomainStore
from mcp_proxy.proxy_mcp import build_proxy_mcp_server
from mcp_proxy.security import AuthEnforcementMiddleware
from mcp_proxy.settings import Settings

try:
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
except ImportError:  # pragma: no cover
    StreamableHTTPSessionManager = None  # type: ignore[misc, assignment]

_MIN_MCP = (1, 24, 0)
_log = logging.getLogger("mcp_proxy.compat")


def _mcp_version_tuple(ver: str) -> tuple[int, int, int]:
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)", ver.strip())
    if not m:
        return (0, 0, 0)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _check_mcp_streamable_http_compat() -> None:
    """Older mcp rejects initialize without mcp-session-id (JSON-RPC -32600 / Missing session ID)."""
    try:
        ver = version("mcp")
    except PackageNotFoundError:  # pragma: no cover
        return
    got = _mcp_version_tuple(ver)
    _log.info("mcp package version %s (need >= %s.%s.%s for streamable HTTP clients)", ver, *_MIN_MCP)
    if got < _MIN_MCP:
        raise RuntimeError(
            f"Installed mcp=={ver} is too old for this proxy. Streamable HTTP clients (e.g. llamacpp webui) "
            f"need mcp>={_MIN_MCP[0]}.{_MIN_MCP[1]}.{_MIN_MCP[2]} (fixes initialize without mcp-session-id). "
            "Rebuild the image without cache: docker compose build --no-cache && docker compose up -d"
        )


class _NormalizeMcpPathASGI:
    """Rewrite bare ``/mcp`` → ``/mcp/`` so ``Mount('/mcp')`` matches (``redirect_slashes`` is off).

    Implemented as plain ASGI: ``BaseHTTPMiddleware`` can forward a stale scope to the router, so POST ``/mcp``
    still 404ed after normalization.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("path") == "/mcp":
            scope = dict(scope)
            scope["path"] = "/mcp/"
            scope["raw_path"] = b"/mcp/"
        await self.app(scope, receive, send)


class _McpStreamableMount:
    """ASGI app mounted at `/mcp` delegating to StreamableHTTPSessionManager."""

    def __init__(self, session_manager: StreamableHTTPSessionManager) -> None:
        self._session_manager = session_manager

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
            return
        if scope["type"] != "http":
            return
        await self._session_manager.handle_request(scope, receive, send)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    (settings.data_dir / "config").mkdir(parents=True, exist_ok=True)
    async with app.state.mcp_session_manager.run():
        yield


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    attach_ring_logging()
    _check_mcp_streamable_http_compat()
    settings.log_auth_state()
    app = FastAPI(
        title="MCP Proxy",
        version="0.1.0",
        redirect_slashes=False,
        description=(
            "MCP proxy: aggregates upstream MCP servers behind Streamable HTTP on /mcp. "
            "LLM clients only see searchToolsForDomain, searchTool, and callTool — discover tools by domain "
            "or search, then callTool with the composite toolName and schema-driven arguments."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.server_store = ServerConfigStore(settings.data_dir)
    app.state.client_store = ClientTokenStore(settings.data_dir)
    app.state.domain_store = DomainStore(settings.data_dir)
    app.state.domain_store.ensure_default_domain()

    if StreamableHTTPSessionManager is None:
        raise RuntimeError("mcp package is missing StreamableHTTPSessionManager; upgrade modelcontextprotocol")
    mcp_sdk_server = build_proxy_mcp_server(app.state.server_store, app.state.domain_store)
    app.state.mcp_session_manager = StreamableHTTPSessionManager(
        mcp_sdk_server,
        stateless=False,
    )

    session_key = (
        settings.session_secret.strip()
        if settings.auth_enabled
        else "dev-no-auth-session-key-do-not-use"
    )
    # Innermost of the HTTP stack (runs just before routing): fix `/mcp` vs `/mcp/` for the mount.
    app.add_middleware(_NormalizeMcpPathASGI)
    app.add_middleware(AuthEnforcementMiddleware, settings=settings)
    app.add_middleware(
        SessionMiddleware,
        secret_key=session_key,
        max_age=60 * 60 * 24 * 7,
        same_site="lax",
        https_only=settings.secure_cookies,
        session_cookie="mcp_proxy_session",
    )
    # Outermost: handle browser CORS preflight before auth middleware sees OPTIONS /mcp.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(api_router, prefix="/api")

    app.mount("/mcp", _McpStreamableMount(app.state.mcp_session_manager))

    @app.get("/")
    async def root() -> RedirectResponse:
        """Bare host:port hits `/`; send users to the admin UI."""
        return RedirectResponse(url="/admin/", status_code=307)

    admin_dir = Path(settings.static_root).resolve() / "admin"
    if admin_dir.is_dir():

        @app.get("/admin", include_in_schema=False)
        async def admin_trailing_slash() -> RedirectResponse:
            """With redirect_slashes=False, `/admin` may not hit the mount; normalize for bookmarks."""
            return RedirectResponse(url="/admin/", status_code=307)

        app.mount(
            "/admin",
            StaticFiles(directory=str(admin_dir), html=True),
            name="admin",
        )

    return app


app = create_app()
