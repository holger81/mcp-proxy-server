from fastapi import APIRouter, Depends

from mcp_proxy.api.auth import router as auth_router
from mcp_proxy.api.catalog import router as catalog_router
from mcp_proxy.api.clients import router as clients_router
from mcp_proxy.api.domains import router as domains_router
from mcp_proxy.api.servers import router as servers_router
from mcp_proxy.security import require_admin_session, require_api_access

router = APIRouter(tags=["api"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


router.include_router(auth_router)

_secured = APIRouter(dependencies=[Depends(require_api_access)])
_secured.include_router(catalog_router)
_secured.include_router(servers_router)
router.include_router(_secured)

_admin = APIRouter(dependencies=[Depends(require_admin_session)])
_admin.include_router(clients_router)
_admin.include_router(domains_router)
router.include_router(_admin)
