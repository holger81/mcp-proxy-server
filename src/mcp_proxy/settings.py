from pathlib import Path
from typing import Annotated

from pydantic import BeforeValidator
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
    """Directory for persisted config and venvs (bind-mount in Docker)."""
    data_dir: Path = Path("/data")
    """Allow POST /api/venvs/install-pypi from the admin UI (disable in untrusted networks)."""
    allow_pypi_install: Annotated[bool, BeforeValidator(_env_bool)] = True
    """Static files root; admin UI lives under `<static_root>/admin/`."""
    static_root: Path = Path("static")
