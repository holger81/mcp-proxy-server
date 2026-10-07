"""Admin session + API bearer authentication."""

from __future__ import annotations

import hashlib
import logging
import secrets
from typing import TYPE_CHECKING

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, RedirectResponse, Response

if TYPE_CHECKING:
    from mcp_proxy.client_store import ApiClientRecord, ClientTokenStore
    from mcp_proxy.settings import Settings

logger = logging.getLogger("mcp_proxy.auth")

SESSION_ADMIN_KEY = "admin"


def _verify_bearer_with_retry(store: ClientTokenStore, token: str) -> bool:
    """Store errors count as *unauthenticated* after one retry (PLAN 4.3).

    Callers map False to their normal 401 path; we never let an exception
    turn into a pass-through or an unlogged 500.
    """
    for attempt in (1, 2):
        try:
            return store.verify_bearer(token)
        except Exception:  # noqa: BLE001 - any store failure
            if attempt == 1:
                continue
            logger.exception(
                "client store failed during bearer verification; "
                "treating request as unauthenticated (fail closed)"
            )
            return False
    return False


def _resolve_bearer_with_retry(store: ClientTokenStore, token: str) -> ApiClientRecord | None:
    """``resolve_bearer`` with one retry; errors resolve to None (PLAN 4.3)."""
    for attempt in (1, 2):
        try:
            return store.resolve_bearer(token)
        except Exception:  # noqa: BLE001 - any store failure
            if attempt == 1:
                continue
            logger.exception(
                "client store failed during bearer resolution; "
                "treating request as unauthenticated (fail closed)"
            )
            return None
    return None


def _should_redirect_browser_to_login(request: Request) -> bool:
    """HTML navigations and generic Accept values get a login redirect; JSON-only clients get 401."""
    if request.method != "GET":
        return False
    accept = (request.headers.get("accept") or "").lower()
    if "text/html" in accept or "application/xhtml+xml" in accept:
        return True
    if (
        "application/json" in accept
        and "text/html" not in accept
        and "application/xhtml+xml" not in accept
    ):
        return False
    return True


def admin_password_digest(password: str, session_secret: str) -> bytes:
    return hashlib.sha256(f"{session_secret}\0{password}".encode("utf-8")).digest()


def verify_admin_password(settings: Settings, attempt: str) -> bool:
    if not settings.auth_enabled:
        return True
    a = admin_password_digest(attempt, settings.session_secret)
    b = admin_password_digest(settings.admin_password, settings.session_secret)
    return secrets.compare_digest(a, b)


def bearer_token(request: Request) -> str | None:
    h = request.headers.get("Authorization")
    if not h or not h.startswith("Bearer "):
        return None
    return h[7:].strip() or None


def is_admin_session(request: Request) -> bool:
    return bool(request.session.get(SESSION_ADMIN_KEY))


def require_admin_session(request: Request) -> None:
    settings: Settings = request.app.state.settings
    if not settings.auth_enabled:
        return
    if is_admin_session(request):
        return
    raise HTTPException(status_code=401, detail="Admin session required")


def require_api_access(request: Request) -> None:
    """Cookie session (admin) or valid API client bearer token."""
    settings: Settings = request.app.state.settings
    if not settings.auth_enabled:
        return
    if is_admin_session(request):
        return
    token = bearer_token(request)
    if token:
        store: ClientTokenStore = request.app.state.client_store
        if _verify_bearer_with_retry(store, token):
            return
    raise HTTPException(
        status_code=401,
        detail="Not authenticated. Sign in to the admin UI or send Authorization: Bearer <token>.",
    )


def require_admin_api(request: Request) -> None:
    """Admin session, or a bearer client explicitly granted ``can_admin`` (PLAN 4.2, D3).

    Plain bearer tokens keep full access to the MCP endpoint and to
    ``require_api_access`` routes; server/catalog management needs the
    per-client toggle.
    """
    settings: Settings = request.app.state.settings
    if not settings.auth_enabled:
        return
    if is_admin_session(request):
        return
    token = bearer_token(request)
    if token:
        store: ClientTokenStore = request.app.state.client_store
        client = _resolve_bearer_with_retry(store, token)
        if client is not None:
            if client.can_admin:
                return
            raise HTTPException(
                status_code=403,
                detail=(
                    "This API client token is read-only for server management. "
                    "Ask an administrator to enable 'Admin API access' "
                    "(can_admin) for this client in the admin UI."
                ),
            )
    raise HTTPException(
        status_code=401,
        detail=(
            "Admin session or admin-capable bearer token required. Sign in to "
            "the admin UI or send Authorization: Bearer <token> for a client "
            "with 'Admin API access' enabled."
        ),
    )


def require_docs_access(request: Request) -> bool:
    """True if request may access /docs or OpenAPI JSON (admin session only)."""
    settings: Settings = request.app.state.settings
    if not settings.auth_enabled:
        return True
    return is_admin_session(request)


class AuthEnforcementMiddleware(BaseHTTPMiddleware):
    """Protect /mcp, /admin (except login), /docs, and /openapi.json when auth is enabled."""

    def __init__(self, app, settings: Settings) -> None:
        super().__init__(app)
        self.settings = settings

    async def dispatch(self, request: Request, call_next) -> Response:
        if not self.settings.auth_enabled:
            return await call_next(request)

        path = request.url.path

        # Remote MCP clients use Bearer tokens; browsers would get JSON 401 unless Accept prefers HTML.
        if path.rstrip("/") == "/mcp":
            # CORS preflight has no Bearer; answer from the app stack (CORSMiddleware + MCP mount).
            if request.method == "OPTIONS":
                return await call_next(request)
            if request.session.get(SESSION_ADMIN_KEY):
                return await call_next(request)
            token = bearer_token(request)
            if token:
                store: ClientTokenStore = request.app.state.client_store
                if _verify_bearer_with_retry(store, token):
                    return await call_next(request)
            if _should_redirect_browser_to_login(request):
                return RedirectResponse(url="/admin/login.html", status_code=302)
            return JSONResponse({"detail": "Not authenticated"}, status_code=401)

        if path.startswith("/admin"):
            # Assets the *login page itself* needs, served unauthenticated.
            # Since PR 7.2 the page scripts are external files: redirecting a
            # script fetch to login.html hands the browser HTML for a <script
            # src>, which its MIME/CSP checks refuse to execute — the login
            # form dies with no server-side error (login.js regression).
            name = path.rsplit("/", 1)[-1]
            if name in ("login.html", "login.js"):
                return await call_next(request)
            if request.session.get(SESSION_ADMIN_KEY):
                return await call_next(request)
            if _should_redirect_browser_to_login(request):
                return RedirectResponse(url="/admin/login.html", status_code=302)
            return JSONResponse({"detail": "Not authenticated"}, status_code=401)

        if path.startswith("/docs") or path == "/openapi.json":
            if request.session.get(SESSION_ADMIN_KEY):
                return await call_next(request)
            if _should_redirect_browser_to_login(request):
                return RedirectResponse(url="/admin/login.html", status_code=302)
            return JSONResponse({"detail": "Not authenticated"}, status_code=401)

        return await call_next(request)
