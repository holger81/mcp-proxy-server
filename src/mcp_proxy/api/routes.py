from fastapi import APIRouter, Depends

from mcp_proxy.api.auth import router as auth_router
from mcp_proxy.api.catalog import router as catalog_router
from mcp_proxy.api.clients import router as clients_router
from mcp_proxy.api.domains import router as domains_router
from mcp_proxy.api.mail_mcp import router as mail_mcp_router
from mcp_proxy.api.portainer_mcp import router as portainer_mcp_router
from mcp_proxy.api.observability import router as observability_router
from mcp_proxy.api.servers import router as servers_router
from mcp_proxy.security import require_admin_api, require_admin_session

router = APIRouter(tags=["api"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


router.include_router(auth_router)

# Server/catalog management: admin session or bearer client with can_admin
# (PLAN 4.2). Plain tokens keep working on /mcp and read-only endpoints.
_admin_api = APIRouter(dependencies=[Depends(require_admin_api)])
_admin_api.include_router(catalog_router)
_admin_api.include_router(servers_router)
router.include_router(_admin_api)

_admin = APIRouter(dependencies=[Depends(require_admin_session)])
_admin.include_router(clients_router)
_admin.include_router(domains_router)
_admin.include_router(observability_router)
_admin.include_router(mail_mcp_router)
_admin.include_router(portainer_mcp_router)
router.include_router(_admin)
