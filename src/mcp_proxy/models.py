import re
import shlex
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def validate_slug_id(v: str) -> str:
    """Same rules as UpstreamServer.id (for venv names, etc.)."""
    v = v.strip().lower()
    if not _SLUG.match(v):
        raise ValueError(
            "must start with a letter or digit, contain only lowercase letters, digits, hyphens"
        )
    return v


def _split_command(v: Any) -> list[str] | None:
    if v is None or v == "":
        return None
    if isinstance(v, list):
        return [str(x) for x in v]
    if isinstance(v, str):
        parts = shlex.split(v.strip())
        return parts or None
    raise TypeError("command must be a string or list of strings")


def coerce_flat_os_env_mapping(v: Any, *, label: str = "env") -> dict[str, str]:
    """Normalize JSON key/value payloads into string-only maps for subprocess/OS env usage.

    - JSON ``null`` drops the key (unset); do not pass the Python ``\"None\"`` string.
    - JSON booleans become lowercase ``\"true\"`` / ``\"false\"`` (not Python ``\"True\"``).
    - Numbers stringify in the usual decimal form.
    - Nested objects/arrays are rejected (explicit rather than ``str(...)`` blobs).
    """
    if v is None:
        return {}
    if not isinstance(v, dict):
        raise ValueError(f"{label} must be a JSON object")
    out: dict[str, str] = {}
    for k, val in v.items():
        if val is None:
            continue
        key = str(k)
        if isinstance(val, bool):
            out[key] = "true" if val else "false"
            continue
        if isinstance(val, (dict, list)):
            raise ValueError(
                f"{label}: nested objects and arrays are not supported; values must be string, "
                "number, boolean, or null"
            )
        out[key] = str(val)
    return out


HttpTransport = Literal["streamable-http", "sse"]


class UpstreamServer(BaseModel):
    """One MCP upstream definition persisted under /data/config/servers.json."""

    id: Annotated[
        str,
        Field(min_length=1, max_length=63, description="Stable slug, e.g. my-fetch"),
    ]
    domain: str = Field(
        default="default",
        description="Logical domain id (from admin Domains tab) for MCP searchToolsForDomain / enums.",
    )
    enabled: bool = True
    type: Literal["stdio", "http"]
    display_name: str | None = Field(
        default=None, description="Optional label in admin UI"
    )
    llm_context: str = Field(
        default="",
        max_length=12000,
        description="Optional text appended to MCP server instructions and to search results for this upstream.",
    )

    url: str | None = Field(
        default=None,
        description="For type=http: MCP endpoint URL (streamable HTTP path or legacy SSE URL).",
    )
    http_transport: HttpTransport | None = Field(
        default=None,
        description=(
            "For type=http: streamable-http (tries one JSON-RPC POST per request first, then full streamable "
            "client), or sse (legacy HTTP+SSE)."
        ),
    )
    headers: dict[str, str] = Field(default_factory=dict)

    command: list[str] | None = Field(default=None, description="stdio argv")
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    stdio_node_inspect: bool = Field(
        default=False,
        description=(
            "stdio only: if True and the executable is node, spawn with "
            "--inspect=0.0.0.0:PORT (or --inspect-brk) for Chrome/VS Code attach."
        ),
    )
    stdio_node_inspect_brk: bool = Field(
        default=False,
        description="stdio only: use --inspect-brk instead of --inspect (pauses at startup).",
    )
    stdio_node_inspect_port: int = Field(
        default=9229,
        ge=1024,
        le=65535,
        description="stdio only: inspector listen port inside the process network namespace.",
    )

    @field_validator("headers", mode="before")
    @classmethod
    def coerce_headers_flat(cls, v: Any) -> dict[str, str]:
        return coerce_flat_os_env_mapping(v, label="headers")

    @field_validator("env", mode="before")
    @classmethod
    def coerce_env_flat(cls, v: Any) -> dict[str, str]:
        return coerce_flat_os_env_mapping(v, label="env")

    @field_validator("id")
    @classmethod
    def id_slug(cls, v: str) -> str:
        return validate_slug_id(v)

    @field_validator("domain")
    @classmethod
    def domain_slug(cls, v: str) -> str:
        return validate_slug_id(v)

    @field_validator("llm_context", mode="before")
    @classmethod
    def strip_llm_context(cls, v: Any) -> str:
        if v is None:
            return ""
        return str(v) if isinstance(v, str) else str(v)

    @field_validator("command", mode="before")
    @classmethod
    def coerce_command(cls, v: Any) -> list[str] | None:
        return _split_command(v)

    @field_validator("http_transport", mode="before")
    @classmethod
    def migrate_legacy_stateless_post(cls, v: Any) -> Any:
        if v == "stateless-post":
            return "streamable-http"
        return v

    @model_validator(mode="after")
    def type_consistency(self) -> "UpstreamServer":
        if self.type == "http":
            if not self.url or not str(self.url).strip():
                raise ValueError("url is required for http servers")
            self.command = None
            self.cwd = None
            self.env = {}
            self.stdio_node_inspect = False
            self.stdio_node_inspect_brk = False
            if self.http_transport is None:
                self.http_transport = "streamable-http"
        else:
            if not self.command:
                raise ValueError(
                    "command is required for stdio servers (non-empty argv)"
                )
            self.url = None
            self.headers = {}
            self.http_transport = None
        return self


class ServerListFile(BaseModel):
    servers: list[UpstreamServer] = Field(default_factory=list)
