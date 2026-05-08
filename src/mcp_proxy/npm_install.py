"""Install npm packages under /data/npm/<slug> for stdio MCP servers."""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
import json

from mcp_proxy.models import validate_slug_id

# Scoped or unscoped package name, optional @version.
_NPM_NAME_SPEC_RE = re.compile(
    r"^(@[a-zA-Z0-9-]+/[a-zA-Z0-9._-]+|[a-zA-Z0-9][a-zA-Z0-9._-]*)(@[a-zA-Z0-9._-]+)?$"
)

# GitHub git spec for npm: github:owner/repo[#ref]
_NPM_GITHUB_SPEC_RE = re.compile(
    r"^github:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:#[A-Za-z0-9_.:/@+-]+)?$"
)

# git+https URL spec for npm: git+https://github.com/owner/repo(.git)?[#ref]
_NPM_GIT_HTTPS_SPEC_RE = re.compile(
    r"^git\+https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?"
    r"(?:#[A-Za-z0-9_.:/@+-]+)?$"
)

# GitHub tarball URL spec (no git needed):
# - https://codeload.github.com/<owner>/<repo>/tar.gz/<ref>
# - https://github.com/<owner>/<repo>/archive/refs/(heads|tags)/<ref>.tar.gz
_NPM_GITHUB_TARBALL_RE = re.compile(
    r"^https://codeload\.github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/tar\.gz/"
    r"[A-Za-z0-9_.:/@+-]+$"
)
_NPM_GITHUB_ARCHIVE_TARBALL_RE = re.compile(
    r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/archive/refs/"
    r"(?:heads|tags)/[A-Za-z0-9_.:/@+-]+\.tar\.gz$"
)

_NPM_NOISE = frozenset({"npm", "npx", "node", "corepack"})


def npm_root(data_dir: Path) -> Path:
    return (data_dir / "npm").resolve()


def _is_githubish_install_spec(spec: str) -> bool:
    s = spec.strip()
    return bool(
        _NPM_GITHUB_SPEC_RE.match(s)
        or _NPM_GIT_HTTPS_SPEC_RE.match(s)
        or _NPM_GITHUB_TARBALL_RE.match(s)
        or _NPM_GITHUB_ARCHIVE_TARBALL_RE.match(s)
    )


def _iter_package_json_paths(node_modules: Path) -> list[Path]:
    """Return candidate package.json paths under node_modules (1 level + scopes)."""
    out: list[Path] = []
    if not node_modules.is_dir():
        return out
    for p in node_modules.iterdir():
        if not p.is_dir():
            continue
        if p.name.startswith("@"):
            for sub in p.iterdir():
                if not sub.is_dir():
                    continue
                pj = sub / "package.json"
                if pj.is_file():
                    out.append(pj)
            continue
        pj = p / "package.json"
        if pj.is_file():
            out.append(pj)
    return out


def _detect_build_prefix(target: Path, *, guess_bin: str | None = None) -> Path | None:
    """Try to locate the installed package dir to run `npm run build` in.

    When installing from git/tarball, `npm install --prefix <target> <spec>` installs the package under
    `<target>/node_modules/<pkg>`, not at `<target>/package.json`.
    """
    node_modules = target / "node_modules"
    candidates = _iter_package_json_paths(node_modules)
    best: Path | None = None
    best_score = -1
    for pj in candidates:
        try:
            pkg = json.loads(pj.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(pkg, dict):
            continue
        scripts = pkg.get("scripts") if isinstance(pkg.get("scripts"), dict) else {}
        has_build = isinstance(scripts, dict) and "build" in scripts
        if not has_build:
            continue
        score = 0
        bin_field = pkg.get("bin")
        if isinstance(bin_field, dict) and guess_bin and guess_bin in bin_field:
            score += 10
        name = pkg.get("name")
        if isinstance(name, str) and guess_bin and guess_bin in name:
            score += 3
        if isinstance(bin_field, (dict, str)):
            score += 1
        if score > best_score:
            best_score = score
            best = pj.parent
    return best


def _npm_run(
    args: list[str],
    *,
    timeout_s: int,
) -> tuple[int, str]:
    proc = subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    parts: list[str] = []
    if proc.stdout:
        parts.append(proc.stdout)
    if proc.stderr:
        parts.append(proc.stderr)
    return proc.returncode, "".join(parts)


def validate_npm_package_spec(spec: str) -> str:
    s = spec.strip()
    if not s or len(s) > 200:
        raise ValueError("npm package spec empty or too long")
    # For git specs we need ':' and '/' and '#' and '+' and '-' and '.'.
    # Block shell metacharacters and quotes; allow '@' for npm scopes and refs.
    if any(c in s for c in ";|&`$()<>\"'\\\n\r\t"):
        raise ValueError("invalid npm package spec")
    # Allow // only for explicit safe URL specs (git+https or GitHub tarballs).
    if (
        s.startswith("git+https://github.com/")
        or s.startswith("https://codeload.github.com/")
        or s.startswith("https://github.com/")
    ):
        if ".." in s or s.startswith("-"):
            raise ValueError("invalid npm package spec")
    else:
        if "//" in s or s.startswith("-") or ".." in s:
            raise ValueError("invalid npm package spec")
    if not (
        _NPM_NAME_SPEC_RE.match(s)
        or _NPM_GITHUB_SPEC_RE.match(s)
        or _NPM_GIT_HTTPS_SPEC_RE.match(s)
        or _NPM_GITHUB_TARBALL_RE.match(s)
        or _NPM_GITHUB_ARCHIVE_TARBALL_RE.match(s)
    ):
        raise ValueError(
            "npm package spec has unsupported shape (use name/@scope/name, github:owner/repo#ref, "
            "git+https://github.com/owner/repo.git#ref, or a GitHub tarball URL)"
        )
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


def _pick_bin(candidates: list[str], guess: str) -> str | None:
    if not candidates:
        return None
    gg = guess.replace("_", "-")
    exact = next((b for b in candidates if b.replace("_", "-") == gg), None)
    return exact or candidates[0]


@dataclass
class NpmInstallResult:
    ok: bool
    log: str
    prefix_path: str
    new_binaries: list[str]
    suggested_command: str | None


def install_npm_prefix(
    data_dir: Path, slug: str, package_spec: str
) -> NpmInstallResult:
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

    guess = _guess_bin_stem(spec).replace("_", "-")

    # GitHub installs often need a build step (dist/ not prepublished), which in turn needs devDependencies.
    # For registry packages we keep the lean install.
    npm_args = ["npm", "install", "--prefix", str(target)]
    if _is_githubish_install_spec(spec):
        npm_args.append("--include=dev")
    npm_args.append(spec)

    code, log = _npm_run(npm_args, timeout_s=600)
    ok = code == 0

    # If this looks like a source install (GitHub/tarball), attempt to build the installed package so
    # its bin target (often dist/) exists.
    if ok and _is_githubish_install_spec(spec):
        build_prefix = _detect_build_prefix(target, guess_bin=guess)
        if build_prefix is None:
            ok = False
            log += (
                "\nCould not locate an installed package with a build script under node_modules/. "
                "For GitHub/tarball installs we need `scripts.build` to produce the CLI entrypoint.\n"
            )
        else:
            # Important: when the GitHub tarball is installed as a dependency, its devDependencies
            # are not installed (so `tsc` might be missing). Install dev deps inside the extracted
            # package dir, then run build.
            dep_code, dep_log = _npm_run(
                ["npm", "install", "--include=dev", "--prefix", str(build_prefix)],
                timeout_s=900,
            )
            if dep_log:
                log += "\n" + dep_log
            if dep_code != 0:
                ok = False
            else:
                build_code, build_log = _npm_run(
                    ["npm", "run", "build", "--prefix", str(build_prefix)],
                    timeout_s=900,
                )
                if build_log:
                    log += "\n" + build_log
                ok = build_code == 0

    after = _list_bin_names(_bin_dir(target))
    new_bins = sorted((after - before) - _NPM_NOISE)
    all_bins = sorted(after - _NPM_NOISE)
    suggested: str | None = None
    pick = _pick_bin(new_bins, guess)
    if pick is None:
        # Reinstalling an already-present package may add no *new* binaries.
        pick = _pick_bin(all_bins, guess)
    if pick:
        p = (_bin_dir(target) / pick).resolve()
        suggested = str(p)

    return NpmInstallResult(
        ok=ok,
        log=log,
        prefix_path=str(target.resolve()),
        new_binaries=new_bins,
        suggested_command=suggested,
    )
