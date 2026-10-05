import logging
import os
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
    allow_pypi_install: Annotated[bool, BeforeValidator(_env_bool)] = True
    allow_npm_install: Annotated[bool, BeforeValidator(_env_bool)] = True
    # PLAN 4.7b: next release will refuse to start when no admin password is
    # set AND the bind host is non-loopback, unless this flag is true.
    allow_no_auth: Annotated[bool, BeforeValidator(_env_bool)] = False
    # PLAN 5.2a: when true, upstream callTool results keep their isError flag
    # and structuredContent. Default false (today's behavior) this release;
    # 5.2b flips the default.
    propagate_tool_errors: Annotated[bool, BeforeValidator(_env_bool)] = False
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

    def log_install_policy(self) -> None:
        """PLAN 4.6a (D4 warn-first): announce the upcoming default flip.

        Behavior is unchanged here — the defaults stay ``True`` this release.
        Next release flips the default to ``false``; set the vars explicitly
        now to be unaffected.
        """
        log = logging.getLogger("mcp_proxy.settings")
        for env_name in ("MCP_PROXY_ALLOW_PYPI_INSTALL", "MCP_PROXY_ALLOW_NPM_INSTALL"):
            if env_name not in os.environ:
                log.warning(
                    "%s is not set: automatic package installs currently default to "
                    "enabled, but the default will change to disabled in the next "
                    "release. Set %s=true to keep the current behavior or =false to "
                    "opt in early.",
                    env_name,
                    env_name,
                )

    def log_error_policy(self) -> None:
        """PLAN 5.2a (D4 warn-first): announce upstream error propagation.

        Behavior unchanged this release — upstream tool errors still arrive
        as successful text. 5.2b flips the default to propagate.
        """
        log = logging.getLogger("mcp_proxy.settings")
        if "MCP_PROXY_PROPAGATE_TOOL_ERRORS" in os.environ:
            return
        log.warning(
            "MCP_PROXY_PROPAGATE_TOOL_ERRORS is not set: upstream tool errors "
            "(isError) currently reach clients as normal text, but from the "
            "next release they will propagate as errors by default (and "
            "structuredContent will be forwarded). Set "
            "MCP_PROXY_PROPAGATE_TOOL_ERRORS=false to keep hiding errors, or "
            "=true to opt in early."
        )

    def log_bind_policy(self) -> None:
        """PLAN 4.7a (D4 warn-first): loud warning for open unauthenticated bind.

        Behavior unchanged this release. Next release refuses to start in
        this state unless ``MCP_PROXY_ALLOW_NO_AUTH=true`` (4.7b).
        """
        log = logging.getLogger("mcp_proxy.settings")
        if self.auth_enabled or self.host in _LOOPBACK_HOSTS:
            return
        if self.allow_no_auth:
            log.info(
                "No admin password and bind host %s, but MCP_PROXY_ALLOW_NO_AUTH=true; "
                "from the next release this flag keeps the proxy starting without auth.",
                self.host,
            )
            return
        bar = "!" * 78
        log.warning(
            "\n%s\n"
            "  INSECURE BIND: no admin password is set and the proxy listens on %s.\n"
            "  Anyone who can reach this address can use and administer all proxied\n"
            "  MCP servers.\n"
            "  Next release, startup will be REFUSED in this state unless you set\n"
            "  MCP_PROXY_ADMIN_PASSWORD, or MCP_PROXY_ALLOW_NO_AUTH=true (deliberate\n"
            "  open deployment), or bind to a loopback host (127.0.0.1).\n"
            "%s",
            bar,
            self.host,
            bar,
        )
