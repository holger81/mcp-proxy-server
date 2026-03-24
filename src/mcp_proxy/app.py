from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from mcp_proxy.api.routes import router as api_router
from mcp_proxy.client_store import ClientTokenStore
from mcp_proxy.config_store import ServerConfigStore
from mcp_proxy.security import AuthEnforcementMiddleware
from mcp_proxy.settings import Settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    (settings.data_dir / "config").mkdir(parents=True, exist_ok=True)
    yield


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    settings.log_auth_state()
    app = FastAPI(
        title="MCP Proxy",
        version="0.1.0",
        description="Aggregates upstream MCP servers; Streamable HTTP on /mcp.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.server_store = ServerConfigStore(settings.data_dir)
    app.state.client_store = ClientTokenStore(settings.data_dir)

    session_key = (
        settings.session_secret.strip()
        if settings.auth_enabled
        else "dev-no-auth-session-key-do-not-use"
    )
    app.add_middleware(AuthEnforcementMiddleware, settings=settings)
    app.add_middleware(
        SessionMiddleware,
        secret_key=session_key,
        max_age=60 * 60 * 24 * 7,
        same_site="lax",
        https_only=settings.secure_cookies,
        session_cookie="mcp_proxy_session",
    )

    app.include_router(api_router, prefix="/api")

    @app.get("/")
    async def root() -> RedirectResponse:
        """Bare host:port hits `/`; send users to the admin UI."""
        return RedirectResponse(url="/admin/", status_code=307)

    @app.get("/mcp")
    async def mcp_get() -> JSONResponse:
        return JSONResponse(
            status_code=405,
            content={"detail": "GET /mcp (SSE) not implemented yet; use POST for JSON-RPC."},
        )

    @app.post("/mcp")
    async def mcp_post() -> JSONResponse:
        return JSONResponse(
            status_code=501,
            content={"detail": "MCP proxy handler not wired yet."},
        )

    admin_dir = Path(settings.static_root).resolve() / "admin"
    if admin_dir.is_dir():
        app.mount(
            "/admin",
            StaticFiles(directory=str(admin_dir), html=True),
            name="admin",
        )

    return app


app = create_app()
