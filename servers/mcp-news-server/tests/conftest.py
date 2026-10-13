"""Shared fixtures for the news-server test suite.

Audit SEC-4 made every fetch re-validate its first hop, which performs real
DNS resolution. The suite is hermetic (respx intercepts the transport layer),
so resolution gets a deterministic fake: hostnames answer with one public
address, IP literals answer with themselves — keeping "redirect to
127.0.0.1"-style tests meaningful while letting ``*.test`` fixture hosts pass
the guard without an allowlist per test.

Tests that need specific DNS behavior (internal answers, NXDOMAIN) monkeypatch
``url_guard.socket.getaddrinfo`` themselves; doing so in the test body
overrides this autouse fixture.
"""

from __future__ import annotations

import ipaddress
import socket

import pytest

from mcp_news_server import url_guard

_PUBLIC_ANSWER = "93.184.216.34"


def _fake_getaddrinfo(host, port, *args, **kwargs):
    try:
        ipaddress.ip_address(host)
        addr = str(host)
    except ValueError:
        addr = _PUBLIC_ANSWER
    family = socket.AF_INET6 if ":" in addr else socket.AF_INET
    return [(family, socket.SOCK_STREAM, 6, "", (addr, int(port or 0)))]


@pytest.fixture(autouse=True)
def hermetic_dns(monkeypatch):
    monkeypatch.setattr(url_guard.socket, "getaddrinfo", _fake_getaddrinfo)
