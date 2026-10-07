import logging
from pathlib import Path
from typing import Annotated, Self

from pydantic import BeforeValidator, Field, field_validator, model_validator
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


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})


def _default_static_root() -> Path:
    # In a repo checkout, resolve against the repo root instead of the current
    # working directory so the admin UI mounts regardless of launch dir.
    # Installed (wheel) layouts keep the old cwd-relative default; Docker sets
    # MCP_PROXY_STATIC_ROOT=/app/static explicitly.
    candidate = Path(__file__).resolve().parents[2] / "static"
    return candidate if candidate.is_dir() else Path("static")


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
    # Docker image path to install-mail-mcp-release.sh; override for custom layouts.
    mail_mcp_install_script: Path = Path("/app/docker/install-mail-mcp-release.sh")
    portainer_mcp_install_script: Path = Path(
        "/app/docker/install-portainer-mcp-release.sh"
    )
    # PLAN 4.6b: installs are opt-in now (default flipped from true).
    allow_pypi_install: Annotated[bool, BeforeValidator(_env_bool)] = False
    allow_npm_install: Annotated[bool, BeforeValidator(_env_bool)] = False
    # PLAN 4.7b: startup is refused when no admin password is set AND the bind
    # host is non-loopback, unless this flag is true (deliberate open deployment).
    allow_no_auth: Annotated[bool, BeforeValidator(_env_bool)] = False
    # PLAN 5.2b: upstream callTool results keep their isError flag and
    # structuredContent (default flipped from false). Set false to hide
    # upstream errors as plain text again.
    propagate_tool_errors: Annotated[bool, BeforeValidator(_env_bool)] = True
    static_root: Path = Field(default_factory=_default_static_root)
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

    # --- LLM context limits (0 = unlimited for *_max_chars; tool lists use explicit defaults) ---
    # Cap searchTool() matches (sorted by relevance).
    tool_search_max_matches: int = Field(default=25, ge=0, le=500)
    # searchToolsForDomain: default page size when `limit` is omitted (filtered or listAll).
    tool_domain_default_limit: int = Field(default=20, ge=1, le=500)
    # searchToolsForDomain: maximum allowed `limit` per request (clamped).
    tool_domain_max_limit: int = Field(default=100, ge=1, le=2000)
    # Truncate tool description in discovery JSON (chars).
    tool_description_max_chars: int = Field(default=0, ge=0, le=100_000)
    # Truncate serverLlmContext in discovery JSON (chars).
    tool_server_llm_context_max_chars: int = Field(default=0, ge=0, le=100_000)
    # If inputSchema serializes larger than this (chars), replace with a stub (LLM may need to raise limit).
    tool_input_schema_max_chars: int = Field(default=0, ge=0, le=500_000)
    # Paginate upstream callTool text responses when longer than this (chars per page). 0 = off.
    call_tool_response_page_chars: int = Field(default=5000, ge=0, le=2_000_000)
    # Legacy hard cap when page_chars is 0; if page_chars > 0 this only applies to sub-page truncation.
    call_tool_response_text_max_chars: int = Field(default=0, ge=0, le=2_000_000)
    # Use compact JSON for searchToolsForDomain / searchTool payloads (fewer tokens).
    tool_discovery_compact_json: Annotated[bool, BeforeValidator(_env_bool)] = False
    # Truncate server instructions string (initialize / tools refresh); 0 = full text.
    instructions_max_chars: int = Field(default=0, ge=0, le=500_000)

    # Background refresh for mcp-news digest caches (see news_digest_refresher.py).
    news_digest_refresh_enabled: Annotated[bool, BeforeValidator(_env_bool)] = True
    # Override RSS/cache data dir; default is NEWS_MCP_DATA_DIR on the mcp-news server or /data/mcp-news.
    news_mcp_data_dir: Path | None = None

    # Upstream execution timeout (seconds) for callTool and admin inspect operations.
    # Some upstream tools (e.g. IMAP move/copy) can take a while on large mailboxes.
    upstream_timeout_s: float = Field(default=300.0, ge=5.0, le=3600.0)
    # PLAN 6.2: per-upstream tools/list TTL cache (0 disables). Config edits
    # self-invalidate (key includes the config fingerprint); upstream tool
    # *content* changes are picked up within this TTL.
    tool_list_cache_ttl_s: float = Field(default=30.0, ge=0.0, le=86400.0)
    # When true (default), composite tool names use only [a-zA-Z0-9_] so strict MCP clients
    # (e.g. Cursor) accept tools/list. Legacy `server/tool` callTool names still work.
    safe_tool_names: Annotated[bool, BeforeValidator(_env_bool)] = True

    @field_validator("news_mcp_data_dir", mode="before")
    @classmethod
    def empty_path_is_none(cls, v: object) -> Path | None:
        if v is None:
            return None
        s = str(v).strip()
        if not s:
            return None
        return Path(s)

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
    def _align_domain_page_limits(self) -> Self:
        if self.tool_domain_default_limit > self.tool_domain_max_limit:
            self.tool_domain_default_limit = self.tool_domain_max_limit
        return self

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

    def enforce_bind_policy(self) -> None:
        """PLAN 4.7b (D4 flip): refuse to start an open, unauthenticated bind.

        4.7a only warned; this is the announced refusal. Escape hatches are
        the same ones the warning named: set an admin password, bind to
        loopback, or ``MCP_PROXY_ALLOW_NO_AUTH=true`` for a deliberate open
        deployment.
        """
        log = logging.getLogger("mcp_proxy.settings")
        if self.auth_enabled or self.host in _LOOPBACK_HOSTS:
            return
        if self.allow_no_auth:
            log.warning(
                "MCP_PROXY_ALLOW_NO_AUTH=true: starting WITHOUT authentication on %s. "
                "Anyone who can reach this address can use and administer all "
                "proxied MCP servers.",
                self.host,
            )
            return
        bar = "!" * 78
        raise ValueError(
            "\n"
            f"{bar}\n"
            "  INSECURE BIND: no admin password is set and the proxy listens on "
            f"{self.host}.\n"
            "  Anyone who can reach this address can use and administer all proxied\n"
            "  MCP servers. Startup is refused in this state.\n"
            "  Fix with ONE of:\n"
            "    MCP_PROXY_ADMIN_PASSWORD (+ MCP_PROXY_SESSION_SECRET), or the *_FILE\n"
            "    variants for Docker secrets,\n"
            "    MCP_PROXY_ALLOW_NO_AUTH=true  (deliberate open deployment),\n"
            "    MCP_PROXY_HOST=127.0.0.1      (loopback only).\n"
            f"{bar}"
        )
