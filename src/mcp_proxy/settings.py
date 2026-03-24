import logging
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
    # If set, read admin password from this file (strip whitespace). Docker/Portainer secrets.
    admin_password_file: str = ""
    # Required when admin_password is set; used for session signing and password hashing.
    session_secret: str = ""
    # If set, read session secret from this file. Overrides MCP_PROXY_SESSION_SECRET when present.
    session_secret_file: str = ""
    # Set true behind HTTPS so cookies get the Secure flag.
    secure_cookies: Annotated[bool, BeforeValidator(_env_bool)] = False

    @field_validator("admin_password", "session_secret", mode="before")
    @classmethod
    def strip_secrets(cls, v: object) -> str:
        if v is None:
            return ""
        return str(v).strip() if isinstance(v, str) else str(v)

    @field_validator("admin_password_file", "session_secret_file", mode="before")
    @classmethod
    def strip_secret_paths(cls, v: object) -> str:
        if v is None:
            return ""
        return str(v).strip() if isinstance(v, str) else str(v)

    @property
    def auth_enabled(self) -> bool:
        return bool(self.admin_password.strip())

    @staticmethod
    def _read_secret_file(path_str: str, var_name: str) -> str:
        p = Path(path_str)
        if not p.is_file():
            raise ValueError(f"{var_name}: not a file or missing: {path_str}")
        return p.read_text(encoding="utf-8").strip()

    @model_validator(mode="after")
    def _load_secrets_from_files(self) -> "Settings":
        if self.admin_password_file:
            self.admin_password = self._read_secret_file(
                self.admin_password_file, "MCP_PROXY_ADMIN_PASSWORD_FILE"
            )
        if self.session_secret_file:
            self.session_secret = self._read_secret_file(
                self.session_secret_file, "MCP_PROXY_SESSION_SECRET_FILE"
            )
        return self

    @model_validator(mode="after")
    def _require_session_secret_with_password(self) -> "Settings":
        if self.auth_enabled and len(self.session_secret.strip()) < 16:
            raise ValueError(
                "MCP_PROXY_SESSION_SECRET (or MCP_PROXY_SESSION_SECRET_FILE) is required "
                "(min 16 characters) when an admin password is set"
            )
        return self

    def log_auth_state(self) -> None:
        log = logging.getLogger("mcp_proxy.settings")
        if self.auth_enabled:
            log.info(
                "Authentication is enabled (admin password and session secret are loaded)."
            )
        else:
            log.warning(
                "Authentication is disabled: set MCP_PROXY_ADMIN_PASSWORD and "
                "MCP_PROXY_SESSION_SECRET (each at least 16 chars for the secret), "
                "or MCP_PROXY_ADMIN_PASSWORD_FILE / MCP_PROXY_SESSION_SECRET_FILE for Docker secrets."
            )
