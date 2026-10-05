"""PR 3.1 (PLAN.md Phase 3): SSRF guard for caller-supplied URLs.

On main, `news_add_rss_feed`/`news_ingest_urls`/`news_curate.extra_urls` and
the `searx_base_url` tool parameter could point at loopback, RFC1918,
cloud-metadata or IPv6-internal addresses. The guard resolves every answer
and fails closed; redirects are re-validated per hop (decision D2 keeps the
operator env var trusted; NEWS_MCP_ALLOW_URLS opts internals back in).
"""

from __future__ import annotations

import asyncio
import socket

import httpx
import pytest
import respx

from mcp_news_server import url_guard
from mcp_news_server.fetchers import fetch_page_metadata
from mcp_news_server.http_util import async_client
from mcp_news_server.url_guard import UrlBlockedError, assert_safe_public_url

WIRE_RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>Wire</title>
<item><title>Alpha wire</title><link>https://wire.test/a</link></item>
</channel></rss>"""


def fake_dns(*answers):
    def _fake(host, port, **kw):
        family = socket.AF_INET6 if ":" in answers[0] else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 0, "", (a, port)) for a in answers]

    return _fake


BLOCKED_LITERALS = [
    "http://127.0.0.1/feed",
    "http://localhost:8080/",
    "http://10.1.2.3/x",
    "http://192.168.0.5/x",
    "http://172.16.9.9/x",
    "http://169.254.169.254/latest/meta-data/",  # cloud metadata
    "http://100.64.0.1/x",  # CGNAT
    "http://[::1]/x",
    "http://[fd00::1]/x",  # IPv6 unique local
    "http://[fe80::1]/x",  # link-local
    "http://0.0.0.0/x",
    "http://[::ffff:127.0.0.1]/x",  # IPv4-mapped loopback
]


@pytest.mark.parametrize(
    "url",
    BLOCKED_LITERALS
    + ["file:///etc/passwd", "gopher://example.com/", "javascript:alert(1)"],
)
def test_blocked_urls_rejected(url):
    with pytest.raises(UrlBlockedError):
        assert_safe_public_url(url)


def test_public_ip_literal_allowed():
    assert_safe_public_url("https://93.184.216.34/feed.xml")


def test_hostname_resolving_public_allowed(monkeypatch):
    monkeypatch.setattr(url_guard.socket, "getaddrinfo", fake_dns("93.184.216.34"))
    assert_safe_public_url("https://example.com/feed.xml")


def test_hostname_resolving_internal_blocked(monkeypatch):
    monkeypatch.setattr(url_guard.socket, "getaddrinfo", fake_dns("10.0.0.8"))
    with pytest.raises(UrlBlockedError, match="internal"):
        assert_safe_public_url("https://evil.test/feed.xml")


def test_any_internal_answer_blocks(monkeypatch):
    # Mixed A/AAAA answers: one internal answer is enough.
    monkeypatch.setattr(
        url_guard.socket, "getaddrinfo", fake_dns("93.184.216.34", "fd00::5")
    )
    with pytest.raises(UrlBlockedError):
        assert_safe_public_url("https://evil.test/feed.xml")


def test_unresolvable_host_blocked(monkeypatch):
    def boom(*a, **kw):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(url_guard.socket, "getaddrinfo", boom)
    with pytest.raises(UrlBlockedError, match="does not resolve"):
        assert_safe_public_url("https://nope.invalid/feed.xml")


def test_allowlist_host(monkeypatch):
    monkeypatch.setenv("NEWS_MCP_ALLOW_URLS", "intranet.corp, 10.2.0.0/16")
    assert_safe_public_url("http://intranet.corp/rss")  # no DNS needed
    assert_safe_public_url("http://10.2.5.7/rss")


def test_allowlist_does_not_open_everything(monkeypatch):
    monkeypatch.setenv("NEWS_MCP_ALLOW_URLS", "intranet.corp")
    with pytest.raises(UrlBlockedError):
        assert_safe_public_url("http://10.2.5.7/rss")


@respx.mock
def test_redirect_hop_to_internal_is_blocked():
    respx.get("http://hop.test/").mock(
        return_value=httpx.Response(302, headers={"location": "http://127.0.0.1/x"})
    )

    async def run():
        async with async_client() as client:
            with pytest.raises(UrlBlockedError, match="internal address"):
                await fetch_page_metadata(client, "http://hop.test/")

    asyncio.run(run())


@respx.mock
def test_public_redirect_chain_still_followed(monkeypatch):
    monkeypatch.setattr(url_guard.socket, "getaddrinfo", fake_dns("93.184.216.34"))
    respx.get("http://hop.test/").mock(
        return_value=httpx.Response(302, headers={"location": "http://hop2.test/rss.xml"})
    )
    respx.get("http://hop2.test/rss.xml").mock(
        return_value=httpx.Response(200, content=WIRE_RSS.encode())
    )

    async def run():
        async with async_client() as client:
            item = await fetch_page_metadata(client, "http://hop.test/")
        # Title comes from the page <title> after following both hops.
        assert item.title == "Wire"

    asyncio.run(run())
