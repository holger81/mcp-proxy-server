from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from mcp_proxy.rate_limit import LoginRateLimiter
from mcp_proxy.security import SESSION_ADMIN_KEY, verify_admin_password

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginBody(BaseModel):
    password: str = Field(min_length=1, max_length=500)


def _login_limiter(request: Request) -> LoginRateLimiter:
    limiter = getattr(request.app.state, "login_rate_limiter", None)
    if limiter is None:  # pragma: no cover - set in create_app
        limiter = LoginRateLimiter()
        request.app.state.login_rate_limiter = limiter
    return limiter


@router.get("/me")
async def auth_me(request: Request) -> dict:
    settings = request.app.state.settings
    return {
        "auth_enabled": settings.auth_enabled,
        "logged_in": (not settings.auth_enabled)
        or bool(request.session.get(SESSION_ADMIN_KEY)),
    }


@router.post("/login")
async def auth_login(request: Request, body: LoginBody) -> dict:
    settings = request.app.state.settings
    if not settings.auth_enabled:
        return {"ok": True, "message": "Authentication is disabled."}
    # Per-client-IP token bucket + escalating lockout (PLAN 4.4). Peer IP,
    # never X-Forwarded-For, so the bucket is not header-spoofable.
    key = request.client.host if request.client else "unknown"
    limiter = _login_limiter(request)
    retry_after = limiter.hit(key)
    if retry_after is not None:
        return JSONResponse(
            status_code=429,
            content={
                "detail": f"Too many login attempts. Try again in {retry_after} seconds."
            },
            headers={"Retry-After": str(int(retry_after))},
        )
    if not verify_admin_password(settings, body.password):
        limiter.record_failure(key)
        raise HTTPException(status_code=401, detail="Invalid password")
    limiter.record_success(key)
    request.session[SESSION_ADMIN_KEY] = True
    return {"ok": True}


@router.post("/logout")
async def auth_logout(request: Request) -> Response:
    request.session.pop(SESSION_ADMIN_KEY, None)
    return Response(status_code=204)
