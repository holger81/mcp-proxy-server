from fastapi import APIRouter

from mcp_proxy.api.catalog import router as catalog_router
from mcp_proxy.api.servers import router as servers_router

router = APIRouter(tags=["api"])
router.include_router(catalog_router)
router.include_router(servers_router)


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
