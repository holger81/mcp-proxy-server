from pathlib import Path
from typing import Annotated

from pydantic import BeforeValidator, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _env_bool(v: object) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off", ""):
        return False
    return bool(s)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MCP_PROXY_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = "0.0.0.0"
    port: int = 8080
    data_dir: Path = Path("/data")  # config, /venvs, /npm
    allow_pypi_install: Annotated[bool, BeforeValidator(_env_bool)] = True
    allow_npm_install: Annotated[bool, BeforeValidator(_env_bool)] = True
    static_root: Path = Path("static")
    # When set (non-empty), admin UI + API (except /api/health) require auth.
    admin_password: str = ""
    # Required when admin_password is set; used for session signing and password hashing.
    session_secret: str = ""
    # Set true behind HTTPS so cookies get the Secure flag.
    secure_cookies: Annotated[bool, BeforeValidator(_env_bool)] = False

    @field_validator("admin_password", "session_secret", mode="before")
    @classmethod
    def strip_secrets(cls, v: object) -> str:
        if v is None:
            return ""
        return str(v).strip() if isinstance(v, str) else str(v)

    @property
    def auth_enabled(self) -> bool:
        return bool(self.admin_password.strip())

    @model_validator(mode="after")
    def _require_session_secret_with_password(self) -> "Settings":
        if self.auth_enabled and len(self.session_secret.strip()) < 16:
            raise ValueError(
                "MCP_PROXY_SESSION_SECRET is required (min 16 characters) when MCP_PROXY_ADMIN_PASSWORD is set"
            )
        return self
