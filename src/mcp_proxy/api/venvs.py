import asyncio

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from mcp_proxy.models import validate_slug_id
from mcp_proxy.pypi_venv import install_into_venv, validate_package_spec

router = APIRouter(prefix="/venvs", tags=["venvs"])


class PypiInstallBody(BaseModel):
    venv_id: str = Field(min_length=1, max_length=63, description="Directory name under /data/venvs")
    package: str = Field(
        min_length=1,
        max_length=200,
        description="PyPI requirement, e.g. unifi-mcp-server or unifi-mcp-server==0.2.4",
    )

    @field_validator("venv_id")
    @classmethod
    def venv_slug(cls, v: str) -> str:
        return validate_slug_id(v)

    @field_validator("package")
    @classmethod
    def pkg(cls, v: str) -> str:
        return validate_package_spec(v)


@router.post("/install-pypi")
async def install_pypi_package(request: Request, body: PypiInstallBody) -> dict:
    """Create /data/venvs/{venv_id} if needed and pip install a package (stdio MCP servers)."""
    settings = request.app.state.settings
    if not settings.allow_pypi_install:
        raise HTTPException(
            status_code=403,
            detail="PyPI install is disabled (MCP_PROXY_ALLOW_PYPI_INSTALL is false).",
        )

    def run():
        return install_into_venv(settings.data_dir, body.venv_id, body.package)

    try:
        result = await asyncio.to_thread(run)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    return {
        "ok": result.ok,
        "log": result.log,
        "venv_path": result.venv_path,
        "new_console_scripts": result.new_console_scripts,
        "suggested_command": result.suggested_command,
    }
