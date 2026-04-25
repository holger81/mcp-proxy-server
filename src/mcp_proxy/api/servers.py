import asyncio
import json
import subprocess
from typing import Annotated, Any, Literal
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, field_validator, model_validator

from mcp_proxy.config_store import ServerConfigStore
from mcp_proxy.models import UpstreamServer, validate_slug_id
from mcp_proxy.npm_install import install_npm_prefix, validate_npm_package_spec
from mcp_proxy.pypi_venv import install_into_venv, validate_package_spec
from mcp_proxy.stdio_package_meta import (
    get_stdio_meta,
    remove_stdio_meta,
    set_stdio_meta,
)
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


def _npm_package_name(spec: str) -> str:
    s = spec.strip()
    if s.startswith("@"):
        i = s.rfind("@")
        if i > s.find("/"):
            return s[:i]
        return s
    return s.split("@", 1)[0]


def _pypi_dist_from_spec(spec: str) -> str:
    s = spec.strip()
    for sep in ("===", "==", ">=", "<=", "!=", "~=", ">", "<"):
        if sep in s:
            s = s.split(sep, 1)[0].strip()
            break
    return s


def _status_from_meta(
    request: Request, server_id: str, meta: dict[str, str]
) -> dict[str, Any]:
    ecosystem = meta["ecosystem"]
    package_spec = meta["package_spec"]
    package_name = (
        _npm_package_name(package_spec)
        if ecosystem == "npm"
        else _pypi_dist_from_spec(package_spec)
    )
    installed_version: str | None = None
    latest_version: str | None = None
    error: str | None = None
    settings = request.app.state.settings

    try:
        if ecosystem == "npm":
            package_json = (
                settings.data_dir
                / "npm"
                / server_id
                / "node_modules"
                / package_name
                / "package.json"
            )
            if package_json.is_file():
                pkg = json.loads(package_json.read_text(encoding="utf-8"))
                v = pkg.get("version")
                installed_version = str(v) if isinstance(v, str) and v.strip() else None
            proc = subprocess.run(
                ["npm", "view", package_name, "version", "--json"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if proc.returncode == 0:
                out = (proc.stdout or "").strip()
                try:
                    parsed = json.loads(out)
                except json.JSONDecodeError:
                    parsed = out.strip('"')
                if isinstance(parsed, str):
                    latest_version = parsed
            else:
                err = (proc.stderr or proc.stdout or "").strip()
                error = err or "failed to query npm registry"
        else:
            pkg_for_pip = _pypi_dist_from_spec(package_spec)
            venv_py = settings.data_dir / "venvs" / server_id / "bin" / "python"
            if venv_py.is_file():
                proc = subprocess.run(
                    [str(venv_py), "-m", "pip", "show", pkg_for_pip],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if proc.returncode == 0:
                    for line in (proc.stdout or "").splitlines():
                        if line.lower().startswith("version:"):
                            installed_version = line.split(":", 1)[1].strip() or None
                            break
            with urlopen(
                f"https://pypi.org/pypi/{pkg_for_pip}/json", timeout=10
            ) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                latest = data.get("info", {}).get("version")
                if isinstance(latest, str) and latest.strip():
                    latest_version = latest
    except (OSError, subprocess.SubprocessError, TimeoutError) as e:
        error = str(e)
    except (HTTPError, URLError, json.JSONDecodeError) as e:
        error = str(e)

    update_available = bool(
        installed_version and latest_version and installed_version != latest_version
    )
    return {
        "server_id": server_id,
        "managed": True,
        "ecosystem": ecosystem,
        "package_spec": package_spec,
        "package_name": package_name,
        "installed_version": installed_version,
        "latest_version": latest_version,
        "update_available": update_available,
        "error": error,
    }


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
    llm_context: str = Field(default="", max_length=12000)
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

    @field_validator("llm_context", mode="before")
    @classmethod
    def llm_ctx(cls, v: Any) -> str:
        if v is None:
            return ""
        return str(v)

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
async def register_stdio_package(
    request: Request, body: RegisterStdioPackageBody
) -> dict:
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
        llm_context=body.llm_context,
        command=suggested,
        cwd=None,
        env=body.env,
    )

    try:
        store.add(server)
    except ValueError:
        store.update(body.server_id, server)
    set_stdio_meta(
        request.app.state.settings.data_dir,
        body.server_id,
        body.ecosystem,
        body.package,
    )

    return {
        "ok": True,
        "registered": True,
        "log": log,
        "suggested_command": suggested,
        "server": server.model_dump(mode="json"),
    }


@router.get("/{server_id}/stdio-package-status")
async def stdio_package_status(request: Request, server_id: str) -> dict:
    store = _store(request)
    server = store.get(server_id)
    if not server:
        raise HTTPException(status_code=404, detail="server not found")
    if server.type != "stdio":
        raise HTTPException(status_code=400, detail="server is not stdio")

    meta = get_stdio_meta(request.app.state.settings.data_dir, server_id)
    if not meta:
        return {
            "server_id": server_id,
            "managed": False,
            "detail": "No package metadata found for this server (it may have been added manually).",
        }
    return await asyncio.to_thread(_status_from_meta, request, server_id, meta)


@router.post("/{server_id}/upgrade-stdio-package")
async def upgrade_stdio_package(request: Request, server_id: str) -> dict:
    store = _store(request)
    server = store.get(server_id)
    if not server:
        raise HTTPException(status_code=404, detail="server not found")
    if server.type != "stdio":
        raise HTTPException(status_code=400, detail="server is not stdio")

    meta = get_stdio_meta(request.app.state.settings.data_dir, server_id)
    if not meta:
        raise HTTPException(
            status_code=400,
            detail="No package metadata found for this server. Reinstall once from Register server to enable upgrades.",
        )
    ecosystem = meta["ecosystem"]
    package_name = (
        _npm_package_name(meta["package_spec"])
        if ecosystem == "npm"
        else _pypi_dist_from_spec(meta["package_spec"])
    )
    upgrade_spec = package_name
    settings = request.app.state.settings

    if ecosystem == "pypi":
        if not settings.allow_pypi_install:
            raise HTTPException(
                status_code=403,
                detail="PyPI install is disabled (MCP_PROXY_ALLOW_PYPI_INSTALL is false).",
            )

        def run_pip():
            return install_into_venv(settings.data_dir, server_id, upgrade_spec)

        result = await asyncio.to_thread(run_pip)
    else:
        if not settings.allow_npm_install:
            raise HTTPException(
                status_code=403,
                detail="npm install is disabled (MCP_PROXY_ALLOW_NPM_INSTALL is false).",
            )

        def run_npm():
            return install_npm_prefix(settings.data_dir, server_id, upgrade_spec)

        result = await asyncio.to_thread(run_npm)

    if not result.ok:
        return {
            "ok": False,
            "upgraded": False,
            "log": result.log,
            "detail": "Upgrade install failed.",
        }

    suggested = result.suggested_command
    if not suggested:
        if not server.command:
            return {
                "ok": False,
                "upgraded": False,
                "log": result.log,
                "detail": "Upgrade succeeded but command could not be determined.",
            }
    else:
        server.command = [suggested]
    store.update(server_id, server)
    set_stdio_meta(
        request.app.state.settings.data_dir,
        server_id,
        ecosystem,
        upgrade_spec,
    )
    status = await asyncio.to_thread(
        _status_from_meta,
        request,
        server_id,
        {"ecosystem": ecosystem, "package_spec": upgrade_spec},
    )
    return {
        "ok": True,
        "upgraded": True,
        "log": result.log,
        "server": server.model_dump(mode="json"),
        "status": status,
    }


@router.delete("/{server_id}")
async def delete_server(request: Request, server_id: str) -> Response:
    store = _store(request)
    if not store.remove(server_id):
        raise HTTPException(status_code=404, detail="server not found")
    remove_stdio_meta(request.app.state.settings.data_dir, server_id)
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
