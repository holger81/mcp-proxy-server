"""PR 1.4: static_root default must not depend on the current directory."""

from __future__ import annotations

from pathlib import Path

from mcp_proxy.settings import Settings


def test_default_static_root_resolves_against_repo_not_cwd(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("MCP_PROXY_STATIC_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)  # a cwd that has no ./static
    settings = Settings(_env_file=None)
    root = Path(settings.static_root)
    assert root.is_absolute()
    assert (root / "admin").is_dir()
