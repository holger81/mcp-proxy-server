from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from mcp_proxy.api.routes import router as api_router
from mcp_proxy.settings import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    app = FastAPI(
        title="MCP Proxy",
        version="0.1.0",
        description="Aggregates upstream MCP servers; Streamable HTTP on /mcp.",
    )
    app.state.settings = settings

    app.include_router(api_router, prefix="/api")

    @app.get("/mcp")
    async def mcp_get() -> JSONResponse:
        # Streamable HTTP: GET may open an SSE stream; not implemented in scaffold.
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
