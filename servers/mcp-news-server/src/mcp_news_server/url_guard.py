"""SSRF guard for caller-supplied URLs (decision D2).

Applies to per-call tool parameters (feed URLs, ``extra_urls``, the
``searx_base_url`` **parameter**). Operator environment configuration such
as ``SEARXNG_BASE_URL`` stays trusted and must not be passed here.

Policy knobs:

- ``NEWS_MCP_ALLOW_URLS``: comma-separated hostnames or CIDR networks that
  deliberately internal targets may use (e.g. ``intranet.corp,10.2.0.0/16``).
- Redirect hops are re-validated through the hook in :mod:`http_util`.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlsplit


class UrlBlockedError(ValueError):
    """A URL must not be fetched (disallowed scheme or internal target)."""


def _allowlist() -> tuple[set[str], list[ipaddress.IPv4Network | ipaddress.IPv6Network]]:
    hosts: set[str] = set()
    nets: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    raw = os.environ.get("NEWS_MCP_ALLOW_URLS", "")
    for part in raw.split(","):
        part = part.strip().lower().rstrip(".")
        if not part:
            continue
        if "/" in part:
            try:
                nets.append(ipaddress.ip_network(part, strict=False))
            except ValueError:
                continue
        else:
            hosts.add(part)
    return hosts, nets


def _is_blocked_ip(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return True  # unparseable address in a DNS answer: fail closed
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return _is_blocked_ip(str(ip.ipv4_mapped))
    if (
        ip.is_unspecified
        or ip.is_loopback
        or ip.is_private  # incl. RFC1918, fc00::/7 ULA
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
    ):
        return True
    # CGNAT (RFC 6598) and benchmarking (RFC 2544) ranges: not flagged by
    # is_private on recent Python, but never legitimate fetch targets.
    return ip.version == 4 and (
        ip in ipaddress.IPv4Network("100.64.0.0/10")
        or ip in ipaddress.IPv4Network("198.18.0.0/15")
    )


def _in_allowed_nets(
    addr: str, nets: list[ipaddress.IPv4Network | ipaddress.IPv6Network]
) -> bool:
    if not nets:
        return False
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return any(ip in net for net in nets)


def assert_safe_public_url(url: str) -> None:
    """Raise :class:`UrlBlockedError` if fetching ``url`` could reach internals.

    Checks the scheme, then every A/AAAA answer the hostname resolves to
    (fail closed on DNS errors and non-IP answers). Raises before any
    request is made.
    """
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https"):
        raise UrlBlockedError(
            f"scheme {parts.scheme!r} is not allowed (use http or https)"
        )
    host = (parts.hostname or "").strip(".").lower()
    if not host:
        raise UrlBlockedError("URL has no host")

    hosts, nets = _allowlist()
    if host in hosts or _in_allowed_nets(host, nets):
        return

    try:
        infos = socket.getaddrinfo(
            host,
            parts.port or (443 if parts.scheme == "https" else 80),
            proto=socket.IPPROTO_TCP,
        )
    except socket.gaierror:
        raise UrlBlockedError(f"host {host!r} does not resolve") from None

    answers = {info[4][0] for info in infos}
    if not answers:
        raise UrlBlockedError(f"host {host!r} does not resolve")
    for addr in answers:
        if _in_allowed_nets(addr, nets):
            continue
        if _is_blocked_ip(addr):
            raise UrlBlockedError(
                f"host {host!r} resolves to a blocked internal address"
            )
