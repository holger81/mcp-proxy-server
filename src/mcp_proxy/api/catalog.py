from fastapi import APIRouter, Request

from mcp_proxy.catalog_loader import presets_as_json

router = APIRouter(prefix="/catalog", tags=["catalog"])


@router.get("/presets")
async def list_catalog_presets(request: Request) -> list[dict]:
    """MCP server templates for the admin UI; extend via /data/config/catalog_presets.json."""
    data_dir = request.app.state.settings.data_dir
    return presets_as_json(data_dir)
