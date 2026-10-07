"""PLAN 4.7b (D4 flip): refuse startup on an open, unauthenticated bind.

4.7a warned for one release; ``enforce_bind_policy`` (called from
``create_app``) now raises unless one of the escape hatches is set:
admin password, loopback bind, or ``MCP_PROXY_ALLOW_NO_AUTH=true``.
"""

from __future__ import annotations

import logging

import pytest

from mcp_proxy.settings import Settings

PW = dict(admin_password="pw", session_secret="s3cret-s3cret-1234")


def test_refuses_open_bind_without_password(monkeypatch) -> None:
    monkeypatch.delenv("MCP_PROXY_ALLOW_NO_AUTH", raising=False)
    s = Settings(host="0.0.0.0")
    assert s.auth_enabled is False
    with pytest.raises(ValueError, match="MCP_PROXY_ALLOW_NO_AUTH"):
        s.enforce_bind_policy()


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_is_allowed(host: str) -> None:
    Settings(host=host).enforce_bind_policy()


def test_auth_enabled_is_allowed() -> None:
    Settings(host="0.0.0.0", **PW).enforce_bind_policy()


def test_allow_no_auth_starts_with_loud_warning(monkeypatch, caplog) -> None:
    monkeypatch.setenv("MCP_PROXY_ALLOW_NO_AUTH", "true")
    s = Settings(host="0.0.0.0")
    assert s.allow_no_auth is True
    with caplog.at_level(logging.WARNING, logger="mcp_proxy.settings"):
        s.enforce_bind_policy()  # must not raise
    assert any(
        "WITHOUT authentication" in r.getMessage() for r in caplog.records
    )
