from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


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
    """Static files root; admin UI lives under `<static_root>/admin/`."""
    static_root: Path = Path("static")
