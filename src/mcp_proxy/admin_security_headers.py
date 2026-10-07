"""PR 7.2: hardened security headers for the static admin UI (``/admin/*``).

The CSP is deliberately strict on scripts: the admin SPA ships as external
JS files only (no inline ``<script>`` blocks), so ``script-src 'self'``
needs no ``'unsafe-inline'`` — an injection that slips past the 7.1 HTML
escaping still cannot execute script. Inline ``style`` *attributes* are used
throughout the markup and cannot run script in modern browsers, so styles
stay permissive rather than rewriting 30+ static attributes.
"""

from __future__ import annotations

from starlette.datastructures import MutableHeaders

ADMIN_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)


class SecurityHeadersASGI:
    """Wrap an ASGI app; add hardening headers to every HTTP response."""

    def __init__(self, app, *, content_security_policy: str = ADMIN_CSP) -> None:
        self.app = app
        self.csp = content_security_policy

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def _send(message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in (
                    ("content-security-policy", self.csp),
                    ("x-content-type-options", "nosniff"),
                    ("referrer-policy", "no-referrer"),
                ):
                    if name not in headers:
                        headers[name] = value
            await send(message)

        await self.app(scope, receive, _send)
