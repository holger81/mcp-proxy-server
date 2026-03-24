"""Preset definitions for the admin “add from catalog” flow."""

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")

HttpTransport = Literal["streamable-http", "sse"]


class McpCatalogPreset(BaseModel):
    """One row in the MCP server catalog (builtin JSON + optional /data merge)."""

    id: str = Field(description="Stable preset key, e.g. unifi-mcp-server")
    display_name: str
    description: str = ""
    source_url: str | None = Field(default=None, description="Project / docs URL")
    package_hint: str | None = Field(
        default=None,
        description="Optional install hint, e.g. pip install …",
    )
    type: Literal["stdio", "http"]
    default_server_id: str = Field(description="Suggested servers.json id (slug)")
    command: str | None = None
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    http_transport: HttpTransport | None = None

    @field_validator("id", "default_server_id")
    @classmethod
    def slug_fields(cls, v: str) -> str:
        v = v.strip().lower()
        if not _SLUG.match(v):
            raise ValueError("must be a lowercase slug (letters, digits, hyphens)")
        return v

    @field_validator("headers", "env", mode="before")
    @classmethod
    def none_to_dict(cls, v: Any) -> dict[str, str]:
        if v is None:
            return {}
        if not isinstance(v, dict):
            raise TypeError("must be an object")
        return {str(k): str(val) for k, val in v.items()}

    @model_validator(mode="after")
    def type_consistency(self) -> "McpCatalogPreset":
        if self.type == "stdio":
            if not self.command or not str(self.command).strip():
                raise ValueError("stdio presets require a non-empty command")
            self.url = None
            self.headers = {}
            self.http_transport = None
        else:
            if not self.url or not str(self.url).strip():
                raise ValueError("http presets require url")
            self.command = None
            self.cwd = None
            self.env = {}
            if self.http_transport is None:
                self.http_transport = "streamable-http"
        return self

    def model_dump_public(self) -> dict[str, Any]:
        """JSON for the admin UI (same shape as stored preset)."""
        return self.model_dump(mode="json", exclude_none=True)
