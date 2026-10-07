"""PLAN 4.6b (D4 flip): package installs are opt-in.

4.6a warned for one release; this is the announced flip —
``allow_pypi_install`` / ``allow_npm_install`` now default to ``False``.
Operators keep installs by setting the env vars explicitly; the block path
in the API names the env var in its 400 detail.
"""

from __future__ import annotations

from mcp_proxy.settings import Settings

VARS = ("MCP_PROXY_ALLOW_PYPI_INSTALL", "MCP_PROXY_ALLOW_NPM_INSTALL")


def test_defaults_are_disabled(monkeypatch) -> None:
    for v in VARS:
        monkeypatch.delenv(v, raising=False)
    settings = Settings()
    assert settings.allow_pypi_install is False
    assert settings.allow_npm_install is False


def test_env_vars_can_re_enable(monkeypatch) -> None:
    monkeypatch.setenv("MCP_PROXY_ALLOW_PYPI_INSTALL", "true")
    monkeypatch.setenv("MCP_PROXY_ALLOW_NPM_INSTALL", "1")
    settings = Settings()
    assert settings.allow_pypi_install is True
    assert settings.allow_npm_install is True


def test_env_vars_can_stay_disabled(monkeypatch) -> None:
    monkeypatch.setenv("MCP_PROXY_ALLOW_PYPI_INSTALL", "false")
    monkeypatch.setenv("MCP_PROXY_ALLOW_NPM_INSTALL", "0")
    settings = Settings()
    assert settings.allow_pypi_install is False
    assert settings.allow_npm_install is False
