from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response

from mcp_proxy.config_store import ServerConfigStore
from mcp_proxy.models import UpstreamServer
from mcp_proxy.upstream_inspect import run_inspect_with_timeout

router = APIRouter(prefix="/servers", tags=["servers"])

InspectKindParam = Literal["tools", "resources", "prompts", "capabilities"]


def _store(request: Request) -> ServerConfigStore:
    return request.app.state.server_store


@router.get("")
async def list_servers(request: Request) -> list[dict]:
    store = _store(request)
    return [s.model_dump(mode="json") for s in store.list_servers()]


@router.post("", status_code=201)
async def add_server(request: Request, body: UpstreamServer) -> dict:
    store = _store(request)
    try:
        store.add(body)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return body.model_dump(mode="json")


@router.delete("/{server_id}")
async def delete_server(request: Request, server_id: str) -> Response:
    store = _store(request)
    if not store.remove(server_id):
        raise HTTPException(status_code=404, detail="server not found")
    return Response(status_code=204)


@router.get("/{server_id}/inspect")
async def inspect_upstream(
    request: Request,
    server_id: str,
    kind: Annotated[
        InspectKindParam,
        Query(
            description=(
                "tools: list_tools; resources: list_resources (URIs / ‘signals’); "
                "prompts: list_prompts (templates); capabilities: initialize / server handshake"
            )
        ),
    ],
) -> dict:
    store = _store(request)
    server = store.get(server_id)
    if not server:
        raise HTTPException(status_code=404, detail="server not found")
    if not server.enabled:
        raise HTTPException(status_code=400, detail="server is disabled")
    try:
        return {"server_id": server_id, **await run_inspect_with_timeout(server, kind)}
    except TimeoutError as e:
        raise HTTPException(status_code=504, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
