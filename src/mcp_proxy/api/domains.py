from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator

from mcp_proxy.domain_store import DomainRecord, DomainStore
from mcp_proxy.models import validate_slug_id

router = APIRouter(prefix="/domains", tags=["domains"])


def _domains(request: Request) -> DomainStore:
    return request.app.state.domain_store


def _servers_in_domain(request: Request, domain_id: str) -> list[str]:
    store = request.app.state.server_store
    return [s.id for s in store.list_servers() if s.domain == domain_id]


@router.get("")
async def list_domains(request: Request) -> list[dict]:
    return _domains(request).list_public()


class CreateDomainBody(BaseModel):
    id: str = Field(min_length=1, max_length=63)
    label: str = Field(min_length=1, max_length=120)

    @field_validator("id")
    @classmethod
    def id_slug(cls, v: str) -> str:
        return validate_slug_id(v)


@router.post("", status_code=201)
async def create_domain(request: Request, body: CreateDomainBody) -> dict:
    store = _domains(request)
    rec = DomainRecord(id=body.id, label=body.label.strip())
    try:
        store.add(rec)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return rec.model_dump(mode="json")


@router.delete("/{domain_id}", status_code=204)
async def delete_domain(request: Request, domain_id: str) -> Response:
    store = _domains(request)
    users = _servers_in_domain(request, validate_slug_id(domain_id))
    if users:
        raise HTTPException(
            status_code=409,
            detail=f"domain is in use by servers: {', '.join(users)}",
        )
    try:
        removed = store.remove(domain_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if not removed:
        raise HTTPException(status_code=404, detail="domain not found")
    return Response(status_code=204)
