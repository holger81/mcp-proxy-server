"""Admin-only portainer-mcp-enhanced binary status and GitHub release installs."""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from mcp_proxy.settings import Settings

router = APIRouter(prefix="/portainer-mcp", tags=["portainer-mcp"])

_LOG = logging.getLogger(__name__)

_VOLUME_REL = Path("portainer-mcp") / "portainer-mcp-enhanced"
_BUNDLED = Path("/opt/portainer-mcp/portainer-mcp-enhanced")
_RUNNER = Path("/usr/local/bin/portainer-mcp-enhanced")

_TAG_VERSION_RE = re.compile(r"^v[0-9A-Za-z][0-9A-Za-z._+-]{0,62}$")


def _normalize_requested_version(raw: str) -> str:
    s = raw.strip()
    if not s:
        raise HTTPException(status_code=400, detail="version is empty")
    low = s.lower()
    if low == "latest":
        return "latest"
    if _TAG_VERSION_RE.fullmatch(s):
        return s
    raise HTTPException(
        status_code=400,
        detail='Use a release tag like "v0.8.0" or "latest".',
    )


def _linux_binary_supported() -> bool:
    if platform.system().lower() != "linux":
        return False
    m = platform.machine().lower()
    return m in ("x86_64", "amd64", "aarch64", "arm64")


def _executable_exists(path: Path) -> bool:
    try:
        return path.is_file() and os.access(path, os.X_OK)
    except OSError:
        return False


def _resolve_install_script(settings: Settings) -> Path:
    return Path(settings.portainer_mcp_install_script)


def _active_binary(settings: Settings) -> tuple[Path | None, str]:
    data_bin = (settings.data_dir / _VOLUME_REL).resolve()
    if _executable_exists(data_bin):
        return data_bin, "volume"
    if _executable_exists(_BUNDLED):
        return _BUNDLED, "bundled"
    return None, "none"


async def _probe_version_line(binary: Path, timeout_s: float = 5.0) -> str | None:
    for argv in ((str(binary), "--version"), (str(binary), "-V"), (str(binary), "-h")):
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except (TimeoutError, OSError):
            continue
        text = (out or b"").decode("utf-8", errors="replace").strip()
        if text:
            first = text.splitlines()[0].strip()
            if first:
                return first[:500]
    return None


@router.get("/status")
async def portainer_mcp_status(request: Request) -> dict[str, object]:
    settings: Settings = request.app.state.settings
    data_bin = (settings.data_dir / _VOLUME_REL).resolve()
    script = _resolve_install_script(settings)
    active, source = _active_binary(settings)
    version_line: str | None = None
    size: int | None = None
    if active is not None:
        version_line = await _probe_version_line(active)
        try:
            size = active.stat().st_size
        except OSError:
            size = None

    return {
        "machine": platform.machine(),
        "linux_binary_supported": _linux_binary_supported(),
        "data_dir": str(settings.data_dir.resolve()),
        "volume_binary_path": str(data_bin),
        "bundled_binary_path": str(_BUNDLED),
        "runner_path": str(_RUNNER),
        "runner_present": _executable_exists(_RUNNER),
        "install_script_path": str(script),
        "install_script_present": script.is_file(),
        "active_binary_path": str(active) if active else None,
        "active_binary_source": source,
        "active_binary_version_text": version_line,
        "active_binary_bytes": size,
        "register_command_hint": (
            "portainer-mcp-enhanced (runner on PATH) or "
            f"{data_bin} with -server / -token flags"
        ),
    }


class PortainerMcpUpdateBody(BaseModel):
    version: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description='GitHub release tag (e.g. v0.8.0) or "latest".',
    )


@router.post("/update")
async def portainer_mcp_update(
    request: Request, body: PortainerMcpUpdateBody
) -> dict[str, object]:
    settings: Settings = request.app.state.settings
    version = _normalize_requested_version(body.version)

    if not _linux_binary_supported():
        raise HTTPException(
            status_code=400,
            detail="portainer-mcp-enhanced Linux amd64/arm64 tarball is not available for this "
            f"platform ({platform.system()} / {platform.machine()}). Install manually under "
            f"{settings.data_dir / 'portainer-mcp'}.",
        )

    script = _resolve_install_script(settings)
    if not script.is_file():
        raise HTTPException(
            status_code=503,
            detail=(
                f"Install script missing ({script}). Binary updates require the Docker image or "
                "install-portainer-mcp-release.sh."
            ),
        )

    dest = (settings.data_dir / "portainer-mcp").resolve()
    dest.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["PORTAINER_MCP_VERSION"] = version
    env["PORTAINER_MCP_INSTALL_DEST"] = str(dest)
    env["TMP"] = os.environ.get("TMPDIR", "/tmp")
    env["DEST"] = str(dest)

    try:
        proc = await asyncio.create_subprocess_exec(
            "sh",
            str(script),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=300.0)
    except TimeoutError:
        _LOG.warning("portainer-mcp install timed out (version=%s)", version)
        raise HTTPException(
            status_code=504,
            detail="Install timed out after 300s (network or GitHub slow).",
        ) from None
    except OSError as e:
        _LOG.exception("portainer-mcp install failed to start")
        raise HTTPException(status_code=500, detail=str(e)) from e

    text = (out or b"").decode("utf-8", errors="replace")
    if proc.returncode != 0:
        _LOG.warning(
            "portainer-mcp install failed rc=%s version=%s output=%s",
            proc.returncode,
            version,
            text[:2000],
        )
        raise HTTPException(
            status_code=502,
            detail={
                "error": "install_failed",
                "version": version,
                "exit_code": proc.returncode,
                "output": text,
            },
        )

    active, source = _active_binary(settings)
    version_line = await _probe_version_line(active) if active else None

    return {
        "ok": True,
        "version_requested": version,
        "active_binary_path": str(active) if active else None,
        "active_binary_source": source,
        "active_binary_version_text": version_line,
        "output": text.strip(),
    }
