from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from mcp_proxy.client_store import ClientTokenStore

router = APIRouter(prefix="/clients", tags=["clients"])


def _store(request: Request) -> ClientTokenStore:
    return request.app.state.client_store


class CreateClientBody(BaseModel):
    label: str = Field(min_length=1, max_length=200)


@router.get("")
async def list_clients(request: Request) -> list[dict]:
    return _store(request).list_public()


@router.post("", status_code=201)
async def create_client(request: Request, body: CreateClientBody) -> dict:
    try:
        _record, plain = _store(request).create(body.label)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {
        "id": _record.id,
        "label": _record.label,
        "created_at": _record.created_at,
        "token": plain,
    }


@router.delete("/{client_id}", status_code=204)
async def delete_client(request: Request, client_id: str) -> Response:
    if not _store(request).remove(client_id):
        raise HTTPException(status_code=404, detail="client not found")
    return Response(status_code=204)
