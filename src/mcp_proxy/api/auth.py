from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from mcp_proxy.security import SESSION_ADMIN_KEY, verify_admin_password

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginBody(BaseModel):
    password: str = Field(min_length=1, max_length=500)


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
    if not verify_admin_password(settings, body.password):
        raise HTTPException(status_code=401, detail="Invalid password")
    request.session[SESSION_ADMIN_KEY] = True
    return {"ok": True}


@router.post("/logout")
async def auth_logout(request: Request) -> Response:
    request.session.pop(SESSION_ADMIN_KEY, None)
    return Response(status_code=204)
