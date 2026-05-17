from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from mcp_proxy.client_store import ClientLlmLimits, ClientTokenStore
from mcp_proxy.proxy_mcp import _llm_limits_excerpt, build_tool_catalog_for_admin
from mcp_proxy.settings import Settings

router = APIRouter(prefix="/clients", tags=["clients"])


def _store(request: Request) -> ClientTokenStore:
    return request.app.state.client_store


class CreateClientBody(BaseModel):
    label: str = Field(min_length=1, max_length=200)


class UpdateClientBody(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=200)
    llm_limits: ClientLlmLimits | None = None
    disabled_tools: list[str] | None = None


@router.get("")
async def list_clients(request: Request) -> list[dict]:
    return _store(request).list_public()


@router.get("/tool-catalog")
async def tool_catalog(request: Request) -> dict:
    """All tools (meta + upstream) for per-client enable/disable UI."""
    store = request.app.state.server_store
    settings: Settings = request.app.state.settings
    tools = await build_tool_catalog_for_admin(store, settings)
    return {
        "tools": tools,
        "global_llm_limits": _llm_limits_excerpt(settings),
    }


@router.get("/{client_id}")
async def get_client(request: Request, client_id: str) -> dict:
    store = _store(request)
    rec = store.get(client_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="client not found")
    settings: Settings = request.app.state.settings
    body = store.to_admin_dict(rec)
    body["global_llm_limits"] = _llm_limits_excerpt(settings)
    return body


@router.patch("/{client_id}")
async def update_client(
    request: Request, client_id: str, body: UpdateClientBody
) -> dict:
    store = _store(request)
    try:
        rec = store.update(
            client_id,
            label=body.label,
            llm_limits=body.llm_limits,
            disabled_tools=body.disabled_tools,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if rec is None:
        raise HTTPException(status_code=404, detail="client not found")
    settings: Settings = request.app.state.settings
    out = store.to_admin_dict(rec)
    out["global_llm_limits"] = _llm_limits_excerpt(settings)
    return out


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
