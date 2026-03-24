"""Install npm packages under /data/npm/<slug> for stdio MCP servers."""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from mcp_proxy.models import validate_slug_id

# Scoped or unscoped package name, optional @version (no URLs / scripts).
_NPM_SPEC_RE = re.compile(
    r"^(@[a-zA-Z0-9-]+/[a-zA-Z0-9._-]+|[a-zA-Z0-9][a-zA-Z0-9._-]*)(@[a-zA-Z0-9._-]+)?$"
)

_NPM_NOISE = frozenset({"npm", "npx", "node", "corepack"})


def npm_root(data_dir: Path) -> Path:
    return (data_dir / "npm").resolve()


def validate_npm_package_spec(spec: str) -> str:
    s = spec.strip()
    if not s or len(s) > 200:
        raise ValueError("npm package spec empty or too long")
    if any(c in s for c in ";|&`$()<>\"'\\\n\r\t"):
        raise ValueError("invalid npm package spec")
    if "//" in s or s.startswith("-") or ".." in s:
        raise ValueError("invalid npm package spec")
    if not _NPM_SPEC_RE.match(s):
        raise ValueError("npm package spec has unsupported shape (use name or @scope/name, optional @version)")
    return s


def _bin_dir(prefix: Path) -> Path:
    return prefix / "node_modules" / ".bin"


def _list_bin_names(bin_dir: Path) -> set[str]:
    if not bin_dir.is_dir():
        return set()
    return {p.name for p in bin_dir.iterdir() if p.is_file() or p.is_symlink()}


def _guess_bin_stem(spec: str) -> str:
    s = spec.strip()
    if s.startswith("@"):
        parts = s[1:].split("/", 1)
        if len(parts) == 2:
            return parts[1].split("@", 1)[0]
    return s.split("@", 1)[0]


@dataclass
class NpmInstallResult:
    ok: bool
    log: str
    prefix_path: str
    new_binaries: list[str]
    suggested_command: str | None


def install_npm_prefix(data_dir: Path, slug: str, package_spec: str) -> NpmInstallResult:
    sid = validate_slug_id(slug)
    spec = validate_npm_package_spec(package_spec)
    root = npm_root(data_dir).resolve()
    target = (root / sid).resolve()
    try:
        target.relative_to(root)
    except ValueError as e:
        raise ValueError("invalid install path") from e

    if shutil.which("npm") is None:
        return NpmInstallResult(
            ok=False,
            log="npm executable not found (Node.js/npm must be installed in the image).",
            prefix_path=str(target),
            new_binaries=[],
            suggested_command=None,
        )

    target.mkdir(parents=True, exist_ok=True)
    before = _list_bin_names(_bin_dir(target))

    proc = subprocess.run(
        ["npm", "install", "--prefix", str(target), spec],
        capture_output=True,
        text=True,
        timeout=600,
    )
    log_parts = []
    if proc.stdout:
        log_parts.append(proc.stdout)
    if proc.stderr:
        log_parts.append(proc.stderr)
    log = "".join(log_parts)
    ok = proc.returncode == 0

    after = _list_bin_names(_bin_dir(target))
    new_bins = sorted((after - before) - _NPM_NOISE)
    guess = _guess_bin_stem(spec).replace("_", "-")
    suggested: str | None = None
    if new_bins:
        pick = next((b for b in new_bins if b.replace("_", "-") == guess.replace("_", "-")), None)
        pick = pick or new_bins[0]
        p = (_bin_dir(target) / pick).resolve()
        suggested = str(p)

    return NpmInstallResult(
        ok=ok,
        log=log,
        prefix_path=str(target.resolve()),
        new_binaries=new_bins,
        suggested_command=suggested,
    )
