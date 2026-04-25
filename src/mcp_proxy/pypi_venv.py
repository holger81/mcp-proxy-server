"""Create per-server virtualenvs under /data/venvs and pip-install MCP packages."""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from mcp_proxy.models import validate_slug_id

# PyPI name + optional extras + optional PEP 440-ish version (no URLs, no shell).
_PACKAGE_SPEC_RE = re.compile(
    r"^[a-zA-Z0-9](?:[a-zA-Z0-9._-]*)(?:\[[a-zA-Z0-9_,]+\])?"
    r"(?:\s*(?:===|==|>=|<=|!=|~=|>|<)\s*[a-zA-Z0-9.*+!_-]+)?$"
)

_VENV_NOISE = frozenset(
    {
        "python",
        "python3",
        "pip",
        "pip3",
        "wheel",
        "activate",
        "activate.csh",
        "activate.fish",
        "Activate.ps1",
        "deactivate",
    }
)


def venvs_root(data_dir: Path) -> Path:
    return (data_dir / "venvs").resolve()


def validate_package_spec(spec: str) -> str:
    s = spec.strip()
    if not s or len(s) > 200:
        raise ValueError("package spec empty or too long")
    if ";" in s or "\n" in s or "\r" in s:
        raise ValueError("invalid package spec")
    if "//" in s or s.startswith("-"):
        raise ValueError("invalid package spec")
    for token in (
        "@ ",
        "git+",
        "file:",
        "path:",
        "http:",
        "https:",
        "$(",
        "`",
        "|",
        "&",
    ):
        if token in s:
            raise ValueError("only plain PyPI names and versions are allowed")
    if not _PACKAGE_SPEC_RE.match(s):
        raise ValueError("package spec has unsupported characters or shape")
    return s


def _bin_names(bin_dir: Path) -> set[str]:
    if not bin_dir.is_dir():
        return set()
    return {p.name for p in bin_dir.iterdir() if p.is_file()}


def _python_exe(venv: Path) -> Path:
    return venv / "bin" / "python"


def _pick_console_script(candidates: list[str], dist_guess: str) -> str | None:
    if not candidates:
        return None
    dg = dist_guess.replace("_", "-")
    exact = next((s for s in candidates if s.replace("_", "-") == dg), None)
    return exact or candidates[0]


def ensure_venv(venv: Path) -> tuple[str, set[str]]:
    """Create venv if missing. Returns (log, bin names after ensure)."""
    log_parts: list[str] = []
    bin_dir = venv / "bin"
    if _python_exe(venv).is_file():
        log_parts.append(f"Using existing venv: {venv}\n")
        return "".join(log_parts), _bin_names(bin_dir)

    venv.parent.mkdir(parents=True, exist_ok=True)
    log_parts.append(f"Creating venv: {venv}\n")
    proc = subprocess.run(
        [sys.executable, "-m", "venv", str(venv)],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if proc.stdout:
        log_parts.append(proc.stdout)
    if proc.stderr:
        log_parts.append(proc.stderr)
    if proc.returncode != 0:
        raise RuntimeError(
            "".join(log_parts) or f"venv creation failed ({proc.returncode})"
        )
    if not _python_exe(venv).is_file():
        raise RuntimeError("venv created but python binary missing")
    return "".join(log_parts), _bin_names(bin_dir)


def pip_install(venv: Path, package_spec: str) -> tuple[str, int]:
    """Run pip install; returns (combined log, return code)."""
    proc = subprocess.run(
        [
            str(_python_exe(venv)),
            "-m",
            "pip",
            "install",
            "--no-cache-dir",
            "--upgrade",
            package_spec,
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    parts = []
    if proc.stdout:
        parts.append(proc.stdout)
    if proc.stderr:
        parts.append(proc.stderr)
    return "".join(parts), proc.returncode


@dataclass
class PypiInstallResult:
    ok: bool
    log: str
    venv_path: str
    new_console_scripts: list[str]
    suggested_command: str | None


def install_into_venv(
    data_dir: Path, venv_id: str, package_spec: str
) -> PypiInstallResult:
    vid = validate_slug_id(venv_id)
    spec = validate_package_spec(package_spec)
    root = venvs_root(data_dir).resolve()
    venv = (root / vid).resolve()
    try:
        venv.relative_to(root)
    except ValueError as e:
        raise ValueError("invalid venv path") from e

    try:
        log, baseline_names = ensure_venv(venv)
        pip_log, code = pip_install(venv, spec)
        log += pip_log
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        return PypiInstallResult(
            ok=False,
            log=str(e),
            venv_path=str(venv),
            new_console_scripts=[],
            suggested_command=None,
        )

    bin_dir = venv / "bin"
    after = _bin_names(bin_dir)
    new_scripts = sorted((after - baseline_names) - _VENV_NOISE)
    all_scripts = sorted(after - _VENV_NOISE)
    name_part = spec.split("[")[0].strip()
    for sep in ("===", "==", ">=", "<=", "!=", "~=", ">", "<"):
        if sep in name_part:
            name_part = name_part.split(sep, 1)[0].strip()
            break
    dist_guess = name_part
    suggested: str | None = None
    pick = _pick_console_script(new_scripts, dist_guess)
    if pick is None:
        # Reinstalling an already-present package may add no *new* scripts.
        pick = _pick_console_script(all_scripts, dist_guess)
    if pick:
        suggested = str((bin_dir / pick).resolve())

    ok = code == 0
    return PypiInstallResult(
        ok=ok,
        log=log,
        venv_path=str(venv.resolve()),
        new_console_scripts=new_scripts,
        suggested_command=suggested,
    )
