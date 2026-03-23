from fastapi import APIRouter, HTTPException, Request, Response

from mcp_proxy.config_store import ServerConfigStore
from mcp_proxy.models import UpstreamServer

router = APIRouter(prefix="/servers", tags=["servers"])


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
