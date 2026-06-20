"""Per API-client tool allow/deny policy and settings merge."""

from __future__ import annotations

from mcp import types as mcp_types
from mcp.shared.exceptions import McpError

from mcp_proxy.client_store import ApiClientRecord
from mcp_proxy.settings import Settings

META_TOOL_NAMES: frozenset[str] = frozenset(
    {"searchToolsForDomain", "searchTool", "callTool", "htmlToPlainText"}
)


def merge_client_settings(
    global_settings: Settings, client: ApiClientRecord | None
) -> Settings:
    """Return effective settings for the current MCP request."""
    if client is None or not client.llm_limits.has_any_override():
        return global_settings
    lim = client.llm_limits
    data = global_settings.model_dump()
    for field_name in lim.model_fields:
        val = getattr(lim, field_name)
        if val is not None:
            data[field_name] = val
    return Settings.model_validate(data)


def client_disabled_tools(client: ApiClientRecord | None) -> frozenset[str]:
    if client is None or not client.disabled_tools:
        return frozenset()
    return frozenset(client.disabled_tools)


def is_tool_disabled(wire_name: str, disabled: frozenset[str]) -> bool:
    return wire_name in disabled


def assert_tool_allowed(wire_name: str, disabled: frozenset[str]) -> None:
    if is_tool_disabled(wire_name, disabled):
        raise McpError(
            mcp_types.ErrorData(
                code=mcp_types.INVALID_PARAMS,
                message=(
                    f"Tool {wire_name!r} is disabled for this API client. "
                    "Ask an administrator to enable it in the proxy admin UI."
                ),
            )
        )
