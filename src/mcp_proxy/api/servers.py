import asyncio
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, field_validator, model_validator

from mcp_proxy.config_store import ServerConfigStore
from mcp_proxy.models import UpstreamServer, validate_slug_id
from mcp_proxy.npm_install import install_npm_prefix, validate_npm_package_spec
from mcp_proxy.pypi_venv import install_into_venv, validate_package_spec
from mcp_proxy.upstream_inspect import run_inspect_with_timeout, upstream_error_detail

router = APIRouter(prefix="/servers", tags=["servers"])

InspectKindParam = Literal["tools", "resources", "prompts", "capabilities"]


def _store(request: Request) -> ServerConfigStore:
    return request.app.state.server_store


def _validate_domain(request: Request, domain_id: str) -> None:
    if domain_id not in request.app.state.domain_store.id_set():
        raise HTTPException(
            status_code=400,
            detail=f"Unknown domain {domain_id!r}. Add it under Admin → Domains.",
        )


@router.get("")
async def list_servers(request: Request) -> list[dict]:
    store = _store(request)
    return [s.model_dump(mode="json") for s in store.list_servers()]


@router.post("", status_code=201)
async def add_server(request: Request, body: UpstreamServer) -> dict:
    _validate_domain(request, body.domain)
    store = _store(request)
    try:
        store.add(body)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return body.model_dump(mode="json")


class RegisterStdioPackageBody(BaseModel):
    """Install a PyPI or npm package under /data and register (or update) a stdio upstream."""

    ecosystem: Literal["pypi", "npm"]
    server_id: str = Field(min_length=1, max_length=63)
    domain: str = Field(default="default", max_length=63)
    package: str = Field(min_length=1, max_length=200)
    display_name: str | None = None
    env: dict[str, str] = Field(default_factory=dict)

    @field_validator("server_id")
    @classmethod
    def server_slug(cls, v: str) -> str:
        return validate_slug_id(v)

    @field_validator("domain")
    @classmethod
    def domain_slug(cls, v: str) -> str:
        return validate_slug_id(v)

    @field_validator("package")
    @classmethod
    def strip_package(cls, v: str) -> str:
        return v.strip()

    @field_validator("display_name", mode="before")
    @classmethod
    def display_opt(cls, v: Any) -> str | None:
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        return str(v).strip()

    @field_validator("env", mode="before")
    @classmethod
    def env_obj(cls, v: Any) -> dict[str, str]:
        if v is None:
            return {}
        if not isinstance(v, dict):
            raise TypeError("env must be a JSON object")
        return {str(k): str(val) for k, val in v.items()}

    @model_validator(mode="after")
    def validate_package(self) -> "RegisterStdioPackageBody":
        if self.ecosystem == "pypi":
            validate_package_spec(self.package)
        else:
            validate_npm_package_spec(self.package)
        return self


@router.post("/register-stdio-package")
async def register_stdio_package(request: Request, body: RegisterStdioPackageBody) -> dict:
    """pip or npm install into /data, then add or replace a stdio server with the detected CLI binary."""
    _validate_domain(request, body.domain)
    settings = request.app.state.settings
    store = _store(request)

    if body.ecosystem == "pypi":
        if not settings.allow_pypi_install:
            raise HTTPException(
                status_code=403,
                detail="PyPI install is disabled (MCP_PROXY_ALLOW_PYPI_INSTALL is false).",
            )

        def run_pip():
            return install_into_venv(settings.data_dir, body.server_id, body.package)

        try:
            result = await asyncio.to_thread(run_pip)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        log = result.log
        suggested = result.suggested_command
        install_ok = result.ok
    else:
        if not settings.allow_npm_install:
            raise HTTPException(
                status_code=403,
                detail="npm install is disabled (MCP_PROXY_ALLOW_NPM_INSTALL is false).",
            )

        def run_npm():
            return install_npm_prefix(settings.data_dir, body.server_id, body.package)

        try:
            result = await asyncio.to_thread(run_npm)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        log = result.log
        suggested = result.suggested_command
        install_ok = result.ok

    if not install_ok:
        return {
            "ok": False,
            "registered": False,
            "log": log,
            "detail": "Install command failed (see log).",
            "suggested_command": suggested,
        }

    if not suggested:
        return {
            "ok": False,
            "registered": False,
            "log": log,
            "detail": "Install succeeded but no new CLI binary was detected under bin/ or node_modules/.bin.",
            "suggested_command": None,
        }

    existing = store.get(body.server_id)
    if existing is not None and existing.type != "stdio":
        raise HTTPException(
            status_code=409,
            detail=f"server id {body.server_id!r} already exists as HTTP; choose another id.",
        )

    server = UpstreamServer(
        id=body.server_id,
        domain=body.domain,
        type="stdio",
        enabled=True,
        display_name=body.display_name,
        command=suggested,
        cwd=None,
        env=body.env,
    )

    try:
        store.add(server)
    except ValueError:
        store.update(body.server_id, server)

    return {
        "ok": True,
        "registered": True,
        "log": log,
        "suggested_command": suggested,
        "server": server.model_dump(mode="json"),
    }


@router.delete("/{server_id}")
async def delete_server(request: Request, server_id: str) -> Response:
    store = _store(request)
    if not store.remove(server_id):
        raise HTTPException(status_code=404, detail="server not found")
    return Response(status_code=204)


@router.put("/{server_id}")
async def update_server(request: Request, server_id: str, body: UpstreamServer) -> dict:
    if body.id != server_id:
        raise HTTPException(
            status_code=400,
            detail="JSON id must match the URL path (server id cannot be changed here)",
        )
    _validate_domain(request, body.domain)
    store = _store(request)
    try:
        store.update(server_id, body)
    except KeyError:
        raise HTTPException(status_code=404, detail="server not found") from None
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return body.model_dump(mode="json")


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
        raise HTTPException(status_code=502, detail=upstream_error_detail(e)) from e
