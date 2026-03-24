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


HttpTransport = Literal["streamable-http", "sse"]


class UpstreamServer(BaseModel):
    """One MCP upstream definition persisted under /data/config/servers.json."""

    id: Annotated[str, Field(min_length=1, max_length=63, description="Stable slug, e.g. my-fetch")]
    domain: str = Field(
        default="default",
        description="Logical domain id (from admin Domains tab) for MCP searchToolsForDomain / enums.",
    )
    enabled: bool = True
    type: Literal["stdio", "http"]
    display_name: str | None = Field(default=None, description="Optional label in admin UI")
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

    @field_validator("headers", "env", mode="before")
    @classmethod
    def none_to_dict(cls, v: Any) -> dict[str, str]:
        if v is None:
            return {}
        if not isinstance(v, dict):
            raise TypeError("headers and env must be objects")
        return {str(k): str(val) for k, val in v.items()}

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
            if self.http_transport is None:
                self.http_transport = "streamable-http"
        else:
            if not self.command:
                raise ValueError("command is required for stdio servers (non-empty argv)")
            self.url = None
            self.headers = {}
            self.http_transport = None
        return self


class ServerListFile(BaseModel):
    servers: list[UpstreamServer] = Field(default_factory=list)
