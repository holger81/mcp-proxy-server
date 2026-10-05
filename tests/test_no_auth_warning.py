"""PR 4.7a (D4 warn-first): loud warning for unauthenticated non-loopback bind.

Behavior unchanged this release — the proxy still starts without a
password; 4.7b turns this into a startup refusal unless
``MCP_PROXY_ALLOW_NO_AUTH=true``.
"""

from __future__ import annotations

import logging

from mcp_proxy.settings import Settings


def test_warns_on_open_bind_without_password(monkeypatch, caplog) -> None:
    monkeypatch.delenv("MCP_PROXY_ALLOW_NO_AUTH", raising=False)
    s = Settings(host="0.0.0.0")
    assert s.auth_enabled is False
    caplog.set_level(logging.WARNING, logger="mcp_proxy.settings")
    s.log_bind_policy()
    msgs = [r.getMessage() for r in caplog.records]
    assert any("INSECURE BIND" in m and "0.0.0.0" in m for m in msgs)
    assert any("MCP_PROXY_ALLOW_NO_AUTH" in m for m in msgs)


def test_silent_on_loopback(monkeypatch, caplog) -> None:
    for host in ("127.0.0.1", "localhost", "::1"):
        s = Settings(host=host)
        caplog.clear()
        s.log_bind_policy()
        assert [r for r in caplog.records if "INSECURE" in r.getMessage()] == []


def test_silent_when_auth_enabled(monkeypatch, caplog) -> None:
    s = Settings(
        host="0.0.0.0",
        admin_password="pw",
        session_secret="s3cret-s3cret-1234",
    )
    s.log_bind_policy()
    assert [r for r in caplog.records if "INSECURE" in r.getMessage()] == []


def test_allow_no_auth_downgrades_to_info(monkeypatch, caplog) -> None:
    monkeypatch.setenv("MCP_PROXY_ALLOW_NO_AUTH", "true")
    s = Settings(host="0.0.0.0")
    assert s.allow_no_auth is True
    caplog.set_level(logging.WARNING, logger="mcp_proxy.settings")
    s.log_bind_policy()
    # No WARNING (info note only) — the refusal in 4.7b will honor this flag.
    assert [r for r in caplog.records if "INSECURE" in r.getMessage()] == []
