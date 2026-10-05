"""PR 4.6a (D4 warn-first): startup warning for the upcoming install-flag default flip.

Behavior itself is unchanged in this release — `allow_pypi_install` /
`allow_npm_install` still default to True; 4.6b flips the default.
"""

from __future__ import annotations

import logging

from mcp_proxy.settings import Settings

VARS = ("MCP_PROXY_ALLOW_PYPI_INSTALL", "MCP_PROXY_ALLOW_NPM_INSTALL")


def test_warns_for_each_unset_var(monkeypatch, caplog) -> None:
    for v in VARS:
        monkeypatch.delenv(v, raising=False)
    settings = Settings()
    assert settings.allow_pypi_install is True  # behavior unchanged (4.6a)
    assert settings.allow_npm_install is True
    with caplog.at_level(logging.WARNING, logger="mcp_proxy.settings"):
        settings.log_install_policy()
    messages = [r.getMessage() for r in caplog.records]
    assert any("MCP_PROXY_ALLOW_PYPI_INSTALL" in m and "default" in m for m in messages)
    assert any("MCP_PROXY_ALLOW_NPM_INSTALL" in m and "default" in m for m in messages)


def test_silent_when_vars_are_set(monkeypatch, caplog) -> None:
    monkeypatch.setenv("MCP_PROXY_ALLOW_PYPI_INSTALL", "false")
    monkeypatch.setenv("MCP_PROXY_ALLOW_NPM_INSTALL", "true")
    settings = Settings()
    assert settings.allow_pypi_install is False
    assert settings.allow_npm_install is True
    with caplog.at_level(logging.WARNING, logger="mcp_proxy.settings"):
        settings.log_install_policy()
    assert [r for r in caplog.records if "ALLOW_" in r.getMessage()] == []
