"""MCP proxy: discovery/execution tools plus server-management tools."""

from __future__ import annotations

import enum
import json
import logging
import os
import threading
import time
from collections.abc import Iterable
from datetime import datetime, timezone
from functools import partial
from typing import Any, Callable
from zoneinfo import ZoneInfo

import anyio
from mcp import types as mcp_types
from mcp.client.session import ClientSession
from mcp.server import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.shared.exceptions import McpError
from pydantic import AnyUrl

from mcp_proxy.config_store import ServerConfigStore
from mcp_proxy.html_plain_text import html_to_plain_text
from mcp_proxy.log_buffer import get_ring_handler
from mcp_proxy.domain_store import DomainStore
from mcp_proxy.models import (
    UpstreamServer,
    _split_command,
    coerce_flat_os_env_mapping,
    validate_slug_id,
)
from mcp_proxy.npm_install import install_npm_prefix, validate_npm_package_spec
from mcp_proxy.pypi_venv import install_into_venv, validate_package_spec
from mcp_proxy.client_policy import (
    META_TOOL_NAMES,
    assert_tool_allowed,
    client_disabled_tools,
    is_tool_disabled,
    merge_client_settings,
)
from mcp_proxy.settings import Settings
from mcp_proxy.tool_call_stats import HOT_TOOL_SLOTS, ToolCallStatsStore
from mcp_proxy.tool_response_cache import ToolResponseCache
from mcp_proxy.upstream_tool_cache import UpstreamToolListCache
from mcp_proxy.tool_response_pagination import (
    apply_upstream_error_semantics,
    paginate_call_tool_response,
    paginate_from_cache,
    parse_call_tool_pagination,
    wrap_hot_tool_as_call_tool,
)
from mcp_proxy.live_mcp_tracker import (
    LiveMcpTracker,
    live_tool_span,
    resolve_current_api_client,
    resolve_current_mcp_admin_session,
)
from mcp_proxy.stdio_package_meta import (
    get_stdio_meta,
    remove_stdio_meta,
    set_stdio_meta,
)
from mcp_proxy.upstream_inspect import (
    _upstream_streams,
    format_upstream_stdio_error,
    upstream_error_detail,
)

log = logging.getLogger(__name__)

_TRUNC_SUFFIX = " …[truncated]"


def _effective_settings(base: Settings) -> Settings:
    return merge_client_settings(base, resolve_current_api_client())


def _disabled_for_request() -> frozenset[str]:
    return client_disabled_tools(resolve_current_api_client())


_ADMIN_DOMAIN_ID = "mcp-tools-administration"


async def _install_in_thread(func: Callable[..., Any], *args: Any) -> Any:
    """Run a blocking pip/npm install, honouring task cancellation (PR 5.9).

    ``abandon_on_cancel=True`` returns control immediately when the caller is
    cancelled (client disconnect, session idle timeout, shutdown) instead of
    waiting out installs of up to 15 minutes; the ``cancel_event`` then makes
    the still-running worker tear the child process group down, so pip/npm
    does not keep running unattended.
    """
    cancel_event = threading.Event()
    try:
        return await anyio.to_thread.run_sync(
            partial(func, *args, cancel_event=cancel_event),
            abandon_on_cancel=True,
        )
    except anyio.get_cancelled_exc_class():
        cancel_event.set()
        raise
_ADMIN_SERVER_ID = "mcp-tools-admin"

# PR 38 (external audit): the proxy-admin tools dispatched by plain name inside
# ``_call_tool_impl``. Every route to them (direct tools/call, ``callTool`` with
# the ``mcp-tools-admin/…`` composite, popular-tool hot shortcuts) recurses into
# that dispatch, so one gate at the top of ``_call_tool_impl`` covers all of them.
# Keep in sync with ``_admin_tool_rows`` via ``_assert_admin_tool_catalog_sync``.
_ADMIN_TOOL_NAMES = frozenset(
    {
        "listServers",
        "setServerEnabled",
        "registerStdioServer",
        "registerManualStdioServer",
        "upgradeStdioServer",
        "removeServer",
        "getLogs",
    }
)

# Warn-once memory for the warn-first phase (keyed client id + tool name).
_warned_mcp_admin_calls: set[tuple[str, str]] = set()


def _deny_mcp_admin_tool(name: str) -> None:
    raise McpError(
        mcp_types.ErrorData(
            code=mcp_types.INVALID_PARAMS,
            message=(
                f"Admin tool {name!r} requires the can_admin scope on this "
                "API client (or an admin UI session). Ask an administrator to "
                "grant it in the proxy admin UI (Clients -> can_admin)."
            ),
        )
    )


def _assert_mcp_admin_allowed(name: str, settings: Settings) -> None:
    """PR 38: /mcp admin tools require ``can_admin`` or an admin UI session.

    Mirrors the HTTP admin API (``require_admin_api``, since 4.2):
    - Admin UI session → allow (even if a plain Bearer is also present).
    - ``can_admin`` bearer → allow.
    - Auth disabled (no bound client, no admin session) → allow (single-user).
    - Auth enabled + unbound identity → deny when enforcing (fail closed;
      never treat a missing bind as "auth off").
    Warn-first per D4 while ``enforce_mcp_admin_tools`` is false.
    """
    if name not in _ADMIN_TOOL_NAMES:
        return
    if resolve_current_mcp_admin_session():
        return
    client = resolve_current_api_client()
    if client is not None and client.can_admin:
        return
    if client is None and not settings.auth_enabled:
        return
    if client is None:
        # Auth is on but this request has no admin session and no API client.
        # Do not fail open (that used to let deleted-token races through).
        if settings.enforce_mcp_admin_tools:
            _deny_mcp_admin_tool(name)
        key = ("<unbound>", name)
        if key not in _warned_mcp_admin_calls:
            _warned_mcp_admin_calls.add(key)
            log.warning(
                "Unbound /mcp request called proxy admin tool %r while auth is "
                "enabled. Denying once enforce is on; check bearer bind / admin "
                "session. Set MCP_PROXY_ENFORCE_MCP_ADMIN_TOOLS=1 to enforce now.",
                name,
            )
        return
    if settings.enforce_mcp_admin_tools:
        _deny_mcp_admin_tool(name)
    key = (client.id, name)
    if key not in _warned_mcp_admin_calls:
        _warned_mcp_admin_calls.add(key)
        log.warning(
            "MCP client %r called proxy admin tool %r without the can_admin "
            "scope. The HTTP admin API already requires it; a future release "
            "will deny this call on /mcp too. Grant the scope or set "
            "MCP_PROXY_ENFORCE_MCP_ADMIN_TOOLS=1 to enforce now.",
            client.id,
            name,
        )


# Built-in MCP resource: proxy host clock when resources/read is called (LLM-friendly “what time is it”).
_PROXY_DATETIME_RESOURCE_URI_STR = "mcp-proxy://meta/current-datetime"


def _proxy_datetime_resource_body() -> str:
    """Plain-text snapshot of the proxy process clock when the resource is read."""
    now_utc = datetime.now(timezone.utc)
    lines = [
        f"utc_iso8601={now_utc.isoformat()}",
        f"unix_timestamp_s={now_utc.timestamp():.3f}",
    ]
    tz_raw = (os.environ.get("TZ") or "").strip()
    if tz_raw:
        try:
            local = datetime.now(ZoneInfo(tz_raw))
            lines.append(f"local_wall_clock_iso8601={local.isoformat()}")
            lines.append(f"tz={tz_raw!r}")
        except Exception:
            lines.append(f"tz_env={tz_raw!r}_invalid_local_time_unavailable")
    else:
        lines.append("note=no_TZ_env_see_utc_only_for_container_local_wall_clock")
    lines.append(
        "hint=this_text_is_generated_when_resources_read_runs_refresh_for_a_new_snapshot"
    )
    return "\n".join(lines) + "\n"


# Composite tool-name helpers live in mcp_proxy.tool_names so policy code can
# import them without a cycle (proxy_mcp already imports client_policy).
from mcp_proxy.tool_names import (  # noqa: F401  (re-exported)
    PROXY_TOOL_SEP as _PROXY_TOOL_SEP,
    decode_proxy_tool_name,
    encode_proxy_tool_name,
)


def format_composite_tool_name(
    server_id: str, upstream_tool: str, settings: Settings
) -> str:
    if settings.safe_tool_names:
        return encode_proxy_tool_name(server_id, upstream_tool)
    return f"{server_id}/{upstream_tool}"


def _truncate_text(text: str, max_chars: int, suffix: str = _TRUNC_SUFFIX) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    keep = max(0, max_chars - len(suffix))
    return text[:keep] + suffix


def _llm_limits_excerpt(settings: Settings) -> dict[str, Any]:
    return {
        "tool_search_max_matches": settings.tool_search_max_matches,
        "tool_domain_default_limit": settings.tool_domain_default_limit,
        "tool_domain_max_limit": settings.tool_domain_max_limit,
        "tool_description_max_chars": settings.tool_description_max_chars,
        "tool_server_llm_context_max_chars": settings.tool_server_llm_context_max_chars,
        "tool_input_schema_max_chars": settings.tool_input_schema_max_chars,
        "call_tool_response_page_chars": settings.call_tool_response_page_chars,
        "call_tool_response_text_max_chars": settings.call_tool_response_text_max_chars,
        "tool_discovery_compact_json": settings.tool_discovery_compact_json,
        "instructions_max_chars": settings.instructions_max_chars,
    }


def _shape_tool_row_for_llm(row: dict[str, Any], settings: Settings) -> dict[str, Any]:
    out = dict(row)
    out.pop("_proxyUpstreamTool", None)
    dmax = settings.tool_description_max_chars
    if dmax > 0:
        out["description"] = _truncate_text(str(out.get("description", "")), dmax)
    ctxmax = settings.tool_server_llm_context_max_chars
    if ctxmax > 0 and "serverLlmContext" in out:
        out["serverLlmContext"] = _truncate_text(str(out["serverLlmContext"]), ctxmax)
    smax = settings.tool_input_schema_max_chars
    if smax > 0:
        schema = out.get("inputSchema")
        raw = json.dumps(schema, default=str) if schema is not None else "{}"
        if len(raw) > smax:
            out["inputSchema"] = {
                "type": "object",
                "additionalProperties": True,
                "_proxySchemaTruncated": True,
                "_proxySchemaApproxChars": len(raw),
                "_proxyHint": (
                    f"Serialized inputSchema exceeded MCP_PROXY_TOOL_INPUT_SCHEMA_MAX_CHARS ({smax}); "
                    "raise that limit for the full JSON Schema."
                ),
            }
    return out


def _json_discovery(payload: Any, settings: Settings) -> str:
    if settings.tool_discovery_compact_json:
        return json.dumps(payload, default=str, separators=(",", ":"))
    return json.dumps(payload, indent=2, default=str)


def _schema_bool(*, description: str, default: bool | None = None) -> dict[str, Any]:
    """JSON Schema fragment: boolean or common string forms (LLM clients often send \"false\")."""
    out: dict[str, Any] = {
        "description": description,
        "anyOf": [{"type": "boolean"}, {"type": "string"}],
    }
    if default is not None:
        out["default"] = default
    return out


def _schema_int(
    *,
    description: str,
    minimum: int | None = None,
    default: int | None = None,
) -> dict[str, Any]:
    """JSON Schema fragment: integer or decimal string (LLM clients often quote numbers)."""
    int_part: dict[str, Any] = {"type": "integer"}
    if minimum is not None:
        int_part["minimum"] = minimum
    out: dict[str, Any] = {
        "description": description,
        "anyOf": [int_part, {"type": "string"}],
    }
    if default is not None:
        out["default"] = default
    return out


def _coerce_bool_arg(key: str, raw: object) -> bool:
    if raw is None:
        return False
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        s = raw.strip().lower()
        if s in ("1", "true", "yes", "on"):
            return True
        if s in ("0", "false", "no", "off", ""):
            return False
        raise McpError(
            mcp_types.ErrorData(
                code=mcp_types.INVALID_PARAMS,
                message=f"{key!r} must be a boolean (got string {raw!r}).",
            )
        )
    if isinstance(raw, (int, float)):
        return bool(raw)
    raise McpError(
        mcp_types.ErrorData(
            code=mcp_types.INVALID_PARAMS,
            message=f"{key!r} must be a boolean.",
        )
    )


def _coerce_str_dict_arg(key: str, raw: object) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise McpError(
            mcp_types.ErrorData(
                code=mcp_types.INVALID_PARAMS,
                message=f"{key!r} must be an object of string key/value pairs.",
            )
        )
    try:
        return coerce_flat_os_env_mapping(raw, label=key)
    except ValueError as e:
        raise McpError(
            mcp_types.ErrorData(
                code=mcp_types.INVALID_PARAMS,
                message=str(e) or "Invalid env mapping",
            )
        ) from e


def _npm_package_name(spec: str) -> str:
    s = spec.strip()
    if s.startswith("@"):
        i = s.rfind("@")
        if i > s.find("/"):
            return s[:i]
        return s
    return s.split("@", 1)[0]


def _pypi_dist_from_spec(spec: str) -> str:
    s = spec.strip()
    for sep in ("===", "==", ">=", "<=", "!=", "~=", ">", "<"):
        if sep in s:
            s = s.split(sep, 1)[0].strip()
            break
    return s


def _parse_domain_pagination(
    args: dict[str, Any], settings: Settings
) -> tuple[int, int]:
    off_raw = args.get("offset", 0)
    try:
        offset = int(off_raw) if off_raw is not None else 0
    except (TypeError, ValueError):
        raise McpError(
            mcp_types.ErrorData(
                code=mcp_types.INVALID_PARAMS,
                message="'offset' must be a non-negative integer.",
            )
        )
    if offset < 0:
        raise McpError(
            mcp_types.ErrorData(
                code=mcp_types.INVALID_PARAMS,
                message="'offset' must be >= 0.",
            )
        )
    max_lim = settings.tool_domain_max_limit
    default_lim = min(settings.tool_domain_default_limit, max_lim)
    lim_raw = args.get("limit")
    if lim_raw is None:
        page_limit = default_lim
    else:
        try:
            page_limit = int(lim_raw)
        except (TypeError, ValueError):
            raise McpError(
                mcp_types.ErrorData(
                    code=mcp_types.INVALID_PARAMS,
                    message="'limit' must be a positive integer.",
                )
            )
        if page_limit < 1:
            raise McpError(
                mcp_types.ErrorData(
                    code=mcp_types.INVALID_PARAMS,
                    message="'limit' must be >= 1.",
                )
            )
    page_limit = min(page_limit, max_lim)
    return offset, page_limit


def _tool_row_matches_query(row: dict[str, Any], q_lower: str) -> bool:
    tn = str(row.get("toolName", "")).lower()
    desc = str(row.get("description", "")).lower()
    return q_lower in tn or q_lower in desc


def _rank_match_key(m: dict[str, Any], q_lower: str) -> tuple[int, str]:
    tn = str(m.get("toolName", "")).lower()
    if tn == q_lower:
        return (0, tn)
    if tn.startswith(q_lower):
        return (1, tn)
    return (2, tn)


def _split_proxy_tool_name(name: str) -> tuple[str, str]:
    try:
        return decode_proxy_tool_name(name)
    except ValueError:
        raise McpError(
            mcp_types.ErrorData(
                code=mcp_types.INVALID_PARAMS,
                message=(
                    f"Invalid toolName {name!r}: pass the exact string from discovery "
                    "(safe `serverid__tool`, hex fallback `serverid__p__<hex>`, or legacy `server/tool`)."
                ),
            )
        ) from None


class ToolRowOutcome(enum.Enum):
    """PR 5.4: distinguish 'gone for good' from 'could not ask right now'."""

    FOUND = "found"
    MISSING = "missing"  # server unknown/disabled or tool not in upstream list → prune
    ERROR = "error"  # transient (timeout/transport failure) → never prune


async def _lookup_tool_row(
    store: ServerConfigStore,
    settings: Settings,
    composite: str,
    tool_cache: UpstreamToolListCache | None = None,
) -> tuple[ToolRowOutcome, dict[str, Any] | None]:
    """Resolve one discovery row for a composite tool name (for hot-tool list enrichment)."""
    try:
        sid, orig = decode_proxy_tool_name(composite)
    except ValueError:
        return ToolRowOutcome.MISSING, None
    if sid == _ADMIN_SERVER_ID:
        for row in _admin_tool_rows(settings):
            if row.get("_proxyUpstreamTool") == orig:
                return ToolRowOutcome.FOUND, _shape_tool_row_for_llm(dict(row), settings)
        return ToolRowOutcome.MISSING, None
    upstream = store.get(sid)
    if upstream is None or not upstream.enabled:
        return ToolRowOutcome.MISSING, None
    try:
        with anyio.fail_after(settings.upstream_timeout_s):
            tools = await _list_upstream_tools_cached(upstream, settings, tool_cache)
    except Exception:
        log.debug("lookup_tool_row: failed for %s", composite, exc_info=True)
        return ToolRowOutcome.ERROR, None
    match = [t for t in tools if t.name == orig]
    if not match:
        return ToolRowOutcome.MISSING, None
    defs = _tool_defs_for_server(upstream, match[:1], settings)
    if not defs:
        return ToolRowOutcome.MISSING, None
    return ToolRowOutcome.FOUND, _shape_tool_row_for_llm(defs[0], settings)


def _composite_tool_list_name(composite: str, settings: Settings) -> str:
    """Canonical Tool.name for a composite (stats keys may still be legacy `server/tool`)."""
    try:
        sid, orig = decode_proxy_tool_name(composite)
    except ValueError:
        return composite
    return format_composite_tool_name(sid, orig, settings)


def _match_hot_stats_key(
    name: str, stats_store: ToolCallStatsStore, settings: Settings
) -> str | None:
    """Return the stats key for a hot shortcut name, if it is among the top candidates."""
    for key in stats_store.top_keys():
        if key == name or _composite_tool_list_name(key, settings) == name:
            return key
    return None


async def _as_hot_call_tool_args(
    name: str,
    args: dict[str, Any],
    store: ServerConfigStore,
    settings: Settings,
    stats_store: ToolCallStatsStore,
    tool_cache: UpstreamToolListCache | None = None,
) -> tuple[str, dict[str, Any], str | None, frozenset[str]]:
    """If ``name`` is a verified popular shortcut, rewrite to callTool.

    Returns ``(name, args, display_composite, declared_params)`` where
    ``declared_params`` are the upstream tool's own inputSchema properties —
    known only on this path, and forwarded to the callTool parse (PR 5.5) so
    an upstream parameter named ``responseOffset/…`` is not peeled as a proxy
    field. Stale shortcuts are pruned and left unchanged.
    """
    no_declared: frozenset[str] = frozenset()
    hot_key = _match_hot_stats_key(name, stats_store, settings)
    if hot_key is None:
        return name, args, None, no_declared
    outcome, row = await _lookup_tool_row(store, settings, hot_key, tool_cache)
    if outcome is not ToolRowOutcome.FOUND:
        # PR 5.4: only prune when the tool is verifiably gone; a flaky or
        # timed-out upstream must not wipe the popular-tool shortcut.
        if outcome is ToolRowOutcome.MISSING and stats_store.remove(hot_key):
            log.info(
                "Pruned stale popular tool shortcut %r on call (not available upstream)",
                hot_key,
            )
        return name, args, None, no_declared
    wire = str(row.get("toolName") or hot_key)
    # PR 5.5: the upstream schema is known here, so a legit upstream parameter
    # named responseOffset/… must not be eaten as a proxy pagination field.
    schema = row.get("inputSchema")
    props = schema.get("properties") if isinstance(schema, dict) else None
    declared = frozenset(props) if isinstance(props, dict) else frozenset()
    return (
        "callTool",
        wrap_hot_tool_as_call_tool(wire, args, declared_params=declared),
        wire,
        declared,
    )


def _hot_tool_from_row(row: dict[str, Any]) -> mcp_types.Tool:
    composite = str(row.get("toolName", ""))
    schema = row.get("inputSchema") or {"type": "object", "additionalProperties": True}
    desc = str(row.get("description", "")).strip()
    suffix = (
        "Popular shortcut: listed because this composite tool was frequently invoked; "
        "equivalent to callTool with this toolName."
    )
    full_desc = f"{desc}\n\n{suffix}" if desc else suffix
    return mcp_types.Tool(name=composite, description=full_desc, inputSchema=schema)


async def _verified_hot_tools(
    store: ServerConfigStore,
    settings: Settings,
    stats_store: ToolCallStatsStore,
    disabled: frozenset[str],
    *,
    slots: int = HOT_TOOL_SLOTS,
    tool_cache: UpstreamToolListCache | None = None,
) -> list[mcp_types.Tool]:
    """Session-level popular shortcuts that still exist upstream (prune stale stats)."""
    out: list[mcp_types.Tool] = []
    for composite in stats_store.ranked_keys():
        if len(out) >= slots:
            break
        wire = _composite_tool_list_name(composite, settings)
        if is_tool_disabled(composite, disabled) or is_tool_disabled(wire, disabled):
            continue
        outcome, row = await _lookup_tool_row(store, settings, composite, tool_cache)
        if outcome is not ToolRowOutcome.FOUND:
            # PR 5.4: prune only on a verified miss, never on transient errors.
            if outcome is ToolRowOutcome.MISSING and stats_store.remove(composite):
                log.info(
                    "Pruned stale popular tool shortcut %r (not available upstream)",
                    composite,
                )
            continue
        out.append(_hot_tool_from_row(row))
    return out


async def _build_session_tool_list(
    store: ServerConfigStore,
    domain_ids: list[str],
    settings: Settings,
    stats_store: ToolCallStatsStore,
    tool_cache: UpstreamToolListCache | None = None,
) -> list[mcp_types.Tool]:
    eff = _effective_settings(settings)
    disabled = _disabled_for_request()
    tools: list[mcp_types.Tool] = []
    for t in build_meta_tool_list(domain_ids, eff):
        if not is_tool_disabled(t.name, disabled):
            tools.append(t)
    tools.extend(
        await _verified_hot_tools(
            store, eff, stats_store, disabled, tool_cache=tool_cache
        )
    )
    return tools


async def _list_upstream_tools(
    server: UpstreamServer, settings: Settings
) -> list[mcp_types.Tool]:
    with anyio.fail_after(settings.upstream_timeout_s):
        async with _upstream_streams(server) as (read_stream, write_stream):
            async with ClientSession(
                read_stream,
                write_stream,
                client_info=mcp_types.Implementation(name="mcp-proxy", version="0.1.0"),
            ) as session:
                await session.initialize()
                res = await session.list_tools()
                return list(res.tools)


async def _list_upstream_tools_cached(
    server: UpstreamServer,
    settings: Settings,
    tool_cache: UpstreamToolListCache | None,
) -> list[mcp_types.Tool]:
    """PR 6.2: TTL-cached discovery per upstream (key includes the config
    fingerprint, so config edits self-invalidate); fetch errors drop the
    entry so a recovered upstream is queried again on the next discovery."""
    if tool_cache is None:
        return await _list_upstream_tools(server, settings)
    hit = tool_cache.get(server)
    if hit is not None:
        return hit
    try:
        tools = await _list_upstream_tools(server, settings)
    except Exception:
        tool_cache.invalidate(server.id)
        raise
    tool_cache.put(server, tools)
    return tools


def _tool_defs_for_server(
    server: UpstreamServer,
    tools: list[mcp_types.Tool],
    settings: Settings,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    note = (server.llm_context or "").strip()
    for t in tools:
        composite = format_composite_tool_name(server.id, t.name, settings)
        row: dict[str, Any] = {
            "toolName": composite,
            "description": (t.description or "").strip(),
            "domain": server.domain,
            "serverId": server.id,
            "inputSchema": t.inputSchema,
        }
        if note:
            row["serverLlmContext"] = note
        out.append(row)
    return out


def _admin_tool_rows(settings: Settings) -> list[dict[str, Any]]:
    def mk(
        upstream_tool: str, description: str, input_schema: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "toolName": format_composite_tool_name(
                _ADMIN_SERVER_ID, upstream_tool, settings
            ),
            "_proxyUpstreamTool": upstream_tool,
            "description": description,
            "domain": _ADMIN_DOMAIN_ID,
            "serverId": _ADMIN_SERVER_ID,
            "inputSchema": input_schema,
        }

    rows = [
        mk(
            "listServers",
            (
                "List configured upstream MCP servers (id, type, domain, enabled, target, and "
                "stdio package metadata when available)."
            ),
            {"type": "object", "properties": {}},
        ),
        mk(
            "setServerEnabled",
            "Enable or disable a configured server.",
            {
                "type": "object",
                "properties": {
                    "serverId": {
                        "type": "string",
                        "description": "Configured server id (slug).",
                    },
                    "enabled": _schema_bool(
                        description="True to enable, false to disable.",
                    ),
                },
                "required": ["serverId", "enabled"],
            },
        ),
        mk(
            "registerStdioServer",
            "Install a PyPI/npm package under /data and create/update a stdio server.",
            {
                "type": "object",
                "properties": {
                    "ecosystem": {"type": "string", "enum": ["pypi", "npm"]},
                    "serverId": {"type": "string"},
                    "domain": {"type": "string"},
                    "package": {"type": "string", "description": "Package spec."},
                    "displayName": {"type": "string"},
                    "llmContext": {"type": "string"},
                    "env": {"type": "object", "additionalProperties": True},
                },
                "required": ["ecosystem", "serverId", "domain", "package"],
            },
        ),
        mk(
            "registerManualStdioServer",
            (
                "Register or update a stdio MCP server from a raw command (no PyPI/npm install). "
                "Use for binaries already on PATH or image-baked CLIs (e.g. mail-mcp). Same as Admin → "
                "Register manual stdio MCP."
            ),
            {
                "type": "object",
                "properties": {
                    "serverId": {"type": "string"},
                    "domain": {"type": "string"},
                    "command": {
                        "type": "string",
                        "description": (
                            "Shell-style argv (quoted segments allowed), e.g. mail-mcp or "
                            "/usr/local/bin/mail-mcp"
                        ),
                    },
                    "displayName": {"type": "string"},
                    "llmContext": {"type": "string"},
                    "env": {"type": "object", "additionalProperties": True},
                    "cwd": {
                        "type": "string",
                        "description": "Optional working directory; omit or empty for none.",
                    },
                    "enabled": _schema_bool(
                        description="Defaults to true.",
                        default=True,
                    ),
                },
                "required": ["serverId", "domain", "command"],
            },
        ),
        mk(
            "upgradeStdioServer",
            "Upgrade an installed stdio server package to the latest version.",
            {
                "type": "object",
                "properties": {"serverId": {"type": "string"}},
                "required": ["serverId"],
            },
        ),
        mk(
            "removeServer",
            "Remove a configured server.",
            {
                "type": "object",
                "properties": {"serverId": {"type": "string"}},
                "required": ["serverId"],
            },
        ),
        mk(
            "getLogs",
            (
                "Read recent proxy log lines from the in-memory ring buffer "
                "(same source as the admin UI Logs tab; resets on container restart). "
                "Optional case-insensitive substring filter, e.g. contains='can_admin'."
            ),
            {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": (
                            "Max lines from the end of the buffer (default 200, max 1000)."
                        ),
                    },
                    "contains": {
                        "type": "string",
                        "description": (
                            "Only return lines containing this substring (case-insensitive)."
                        ),
                    },
                },
            },
        ),
    ]
    catalog = {str(r["_proxyUpstreamTool"]) for r in rows}
    if catalog != _ADMIN_TOOL_NAMES:
        missing = sorted(_ADMIN_TOOL_NAMES - catalog)
        extra = sorted(catalog - _ADMIN_TOOL_NAMES)
        raise RuntimeError(
            "Admin tool catalog out of sync with _ADMIN_TOOL_NAMES "
            f"(missing_from_rows={missing!r}, extra_in_rows={extra!r})."
        )
    return rows


async def _collect_all_tool_defs(
    store: ServerConfigStore,
    settings: Settings,
    domain_id: str | None,
    tool_cache: UpstreamToolListCache | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """All tool rows for a domain plus *degraded* upstreams that failed.

    PR 5.6: a failed upstream used to vanish silently from discovery results;
    callers now surface ``[{"serverId": …, "error": …}]`` so an LLM knows the
    tool list may be incomplete instead of hallucinating "tool does not exist".

    PR 6.2: enabled upstreams are queried **concurrently** under one shared
    deadline (``upstream_timeout_s`` for the whole fan-out; previously each
    sequential connect consumed its own timeout budget) and through the
    per-server tool-list TTL cache.
    """
    combined: list[dict[str, Any]] = []
    degraded: list[dict[str, str]] = []
    if domain_id in (None, _ADMIN_DOMAIN_ID):
        combined.extend(_admin_tool_rows(settings))
    servers = [
        s
        for s in store.list_servers()
        if s.enabled and (domain_id is None or s.domain == domain_id)
    ]
    fetched: dict[str, list[mcp_types.Tool]] = {}
    errors: dict[str, str] = {}

    async def _one(s: UpstreamServer) -> None:
        try:
            fetched[s.id] = await _list_upstream_tools_cached(s, settings, tool_cache)
        except TimeoutError:
            log.warning("collect tools: timeout for upstream %s", s.id)
            errors[s.id] = "timeout"
        except Exception as e:
            detail = upstream_error_detail(e)
            log.warning("collect tools: skip upstream %s (%s)", s.id, detail)
            errors[s.id] = detail[:200]

    if servers:
        with anyio.move_on_after(settings.upstream_timeout_s) as deadline:
            async with anyio.create_task_group() as tg:
                for s in servers:
                    tg.start_soon(_one, s)
        if deadline.cancelled_caught:
            log.warning(
                "collect tools: shared deadline (%.0fs) hit; %d upstream(s) unfinished",
                settings.upstream_timeout_s,
                len(servers) - len(fetched) - len(errors),
            )
    for s in servers:  # config order preserved (was sequential before 6.2)
        if s.id in fetched:
            combined.extend(_tool_defs_for_server(s, fetched[s.id], settings))
        else:
            degraded.append({"serverId": s.id, "error": errors.get(s.id, "timeout")})
    disabled = _disabled_for_request()
    if disabled:
        combined = [
            d
            for d in combined
            if not is_tool_disabled(str(d.get("toolName", "")), disabled)
        ]
    return combined, degraded


async def build_tool_catalog_for_admin(
    store: ServerConfigStore,
    settings: Settings,
    tool_cache: UpstreamToolListCache | None = None,
) -> list[dict[str, Any]]:
    """All proxy tools for admin per-client enable/disable UI (ignores client policy)."""
    meta_desc = {
        "searchToolsForDomain": "Search tools within one domain (query + pagination).",
        "searchTool": "Search tools across domains.",
        "callTool": "Execute an upstream tool by composite toolName.",
        "htmlToPlainText": "Extract readable plain text from an HTML string.",
    }
    catalog: list[dict[str, Any]] = [
        {
            "toolName": meta,
            "kind": "meta",
            "domain": None,
            "serverId": None,
            "description": meta_desc.get(meta, ""),
        }
        for meta in sorted(META_TOOL_NAMES)
    ]
    defs, _degraded = await _collect_all_tool_defs(store, settings, None, tool_cache)
    for row in defs:
        sid = str(row.get("serverId", ""))
        catalog.append(
            {
                "toolName": str(row.get("toolName", "")),
                "kind": "admin" if sid == _ADMIN_SERVER_ID else "upstream",
                "domain": row.get("domain"),
                "serverId": sid or None,
                "description": str(row.get("description", "")),
            }
        )
    return catalog


def _domain_enum_schema(domain_ids: list[str], description: str) -> dict[str, Any]:
    if not domain_ids:
        domain_ids = ["default"]
    return {"type": "string", "enum": domain_ids, "description": description}


def _base_instructions() -> str:
    return (
        "You are connected through an MCP proxy. Individual upstream tools are NOT listed at the top level. "
        "As an LLM, you should usually use the discovery/execution tools in sequence when you need a capability "
        "(e.g. smart home, network, or other MCP backends registered in the proxy).\n\n"
        "Workflow:\n"
        "1) Choose a domain id from the `domain` enum on searchToolsForDomain / searchTool (refreshed each "
        "tools/list). Domains group upstream servers (configured in the proxy admin UI), plus a built-in "
        f"admin domain `{_ADMIN_DOMAIN_ID}`.\n"
        "2) Discover tools: call searchToolsForDomain(domain, query) with a specific substring to limit results "
        "(name/description, case-insensitive); responses are paginated (offset/limit, hasMore). "
        "Only when you truly need the full catalog, set listAll=true and page through every tool in that domain "
        "(same pagination). "
        "Or use searchTool(query, domain optional) to search across domains.\n"
        "3) Read the JSON response: each entry includes toolName, description, domain, serverId, inputSchema, "
        "and optionally serverLlmContext (admin notes for that upstream). "
        "Use inputSchema to know required and optional parameters.\n"
        "4) Execute: call callTool with toolName exactly as returned by discovery "
        "(default: safe `serverid__upstream_tool` with only letters, digits, and underscores; hyphens in server "
        "ids become underscores. If the upstream tool name needs other characters, the proxy uses "
        "`serverid__p__` plus hex(UTF-8) instead. Legacy `server/tool` is still accepted by callTool. "
        "Arguments: JSON object matching that schema. "
        "Large text responses are split into pages (see responseCacheId / responseOffset on callTool).\n\n"
        "Built-in utility: `htmlToPlainText` strips HTML to readable plain text (e.g. after fetching "
        "`body_html` from an email tool when `body_text` is empty).\n\n"
        "Resources (optional): MCP resources/list exposes `mcp-proxy://meta/current-datetime`; "
        "resources/read on that URI returns the proxy host clock (UTC ISO 8601 and unix time; optional local "
        "wall-clock line when the container sets TZ). Use it when you need the current date/time for scheduling "
        "or relative dates.\n\n"
        "The proxy also lists up to three frequently invoked composite tools at the session level (same names as "
        "in discovery): you may call them directly with arguments shaped like the upstream inputSchema, or use "
        "callTool as usual.\n\n"
        "Proxy management tools are exposed through the same discovery flow under domain "
        f"`{_ADMIN_DOMAIN_ID}` (serverId `{_ADMIN_SERVER_ID}`); discover with searchToolsForDomain/searchTool, "
        "then execute via callTool.\n\n"
        "Do not invent tool names. Always obtain toolName from searchToolsForDomain or searchTool first. "
        "If unsure which domain applies, try searchTool with a broad query across all domains (omit domain), "
        "then narrow down."
    )


def full_instructions_for_store(
    store: ServerConfigStore,
    instructions_max_chars: int = 0,
    *,
    client_instructions: str = "",
) -> str:
    parts = [
        _base_instructions(),
        "",
        "## Upstream server notes (admin → LLM context)",
        "",
    ]
    any_note = False
    for s in sorted(store.list_servers(), key=lambda x: x.id):
        note = (s.llm_context or "").strip()
        if not note:
            continue
        any_note = True
        title = f"{s.id} ({s.display_name})" if s.display_name else s.id
        parts.append(f"### {title}")
        parts.append(note)
        parts.append("")
    if not any_note:
        parts.append(
            '_No per-server notes yet. Configure "LLM / instructions" for each server in the admin UI._'
        )
        parts.append("")
    client_note = (client_instructions or "").strip()
    if client_note:
        parts.extend(
            [
                "## Client-specific instructions (admin)",
                "",
                client_note,
                "",
            ]
        )
    text = "\n".join(parts)
    return (
        _truncate_text(text, instructions_max_chars)
        if instructions_max_chars > 0
        else text
    )


def _instructions_for_mcp_request(
    store: ServerConfigStore, settings: Settings
) -> str:
    eff = _effective_settings(settings)
    client = resolve_current_api_client()
    client_note = (client.instructions or "").strip() if client else ""
    return full_instructions_for_store(
        store,
        eff.instructions_max_chars,
        client_instructions=client_note,
    )


def session_initialization_options(
    server: Server, store: ServerConfigStore, settings: Settings
) -> Any:
    """Per-session InitializationOptions with request-scoped instructions (PR 5.7).

    Must be called *while handling the initialize HTTP request* (the session
    manager does that), where ``current_mcp_api_client`` is the requesting
    client. Replaces the old pattern of assigning ``server.instructions``
    inside ``list_tools`` — a shared mutable that leaked one client's custom
    instructions into every other client's initialize response.
    """
    base = server.create_initialization_options()
    return base.model_copy(
        update={"instructions": _instructions_for_mcp_request(store, settings)}
    )


def build_meta_tool_list(
    domain_ids: list[str], settings: Settings
) -> list[mcp_types.Tool]:
    dom = _domain_enum_schema(
        domain_ids,
        "Domain id (unique). Choose one; configure domains in the proxy admin UI.",
    )
    dom_opt = _domain_enum_schema(
        domain_ids,
        "Optional: restrict search to this domain id.",
    )
    return [
        mcp_types.Tool(
            name="searchToolsForDomain",
            description=(
                "For LLMs: discovery inside one domain. Prefer a non-empty `query` substring (tool name or "
                "description) so results stay small; each response is one page with pagination metadata (hasMore, "
                "offset, total). "
                "Set listAll=true only when you must enumerate every tool in the domain, then paginate with "
                "offset until hasMore is false. "
                "Then call callTool with toolName from tools[]."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "domain": dom,
                    "query": {
                        "type": "string",
                        "description": (
                            "Substring filter on tool name and description (case-insensitive). "
                            "Required unless listAll is true. Use specific terms to limit context."
                        ),
                    },
                    "listAll": _schema_bool(
                        description=(
                            "If true, return all tools in the domain in pages (sorted by toolName). "
                            "Omit or empty `query`. Increase offset to read the full catalog."
                        ),
                        default=False,
                    ),
                    "offset": _schema_int(
                        description="Pagination: skip this many tools after sort/filter.",
                        minimum=0,
                        default=0,
                    ),
                    "limit": _schema_int(
                        description="Pagination: max tools in this response (server caps). Omit for default.",
                        minimum=1,
                    ),
                },
                "required": ["domain"],
            },
        ),
        mcp_types.Tool(
            name="searchTool",
            description=(
                "For LLMs: discovery when you have a keyword (e.g. 'light', 'wifi') but not the exact tool. "
                "Returns JSON matches with toolName, domain, serverId, inputSchema, and optional serverLlmContext. "
                "Optional `domain` limits search to one domain. "
                "After picking a tool, call callTool with that toolName and arguments from inputSchema."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Substring to match (case-insensitive).",
                    },
                    "domain": dom_opt,
                },
                "required": ["query"],
            },
        ),
        mcp_types.Tool(
            name="callTool",
            description=(
                "For LLMs: execution step only. "
                "Pass toolName exactly as returned by searchToolsForDomain or searchTool. "
                + (
                    "Default format is safe for strict clients: `serverid__tool` (letters, digits, underscores; "
                    "hyphens in server id become underscores), or `serverid__p__<hex>` when the upstream name is not "
                    "fully representable that way. "
                    "Legacy `server/tool` is still accepted. "
                    if settings.safe_tool_names
                    else "Format: `<server-id>/<upstream-tool-name>`. "
                )
                + "Pass arguments as a JSON object; shape must match the tool's inputSchema from the search result. "
                "Oversized text replies are paginated: the JSON body includes pagination.responseCacheId. "
                "Pass responseCacheId and responseOffset on callTool (top level) or on the composite tool "
                "call (same fields as upstream args); the proxy strips them before invoking upstream. "
                "If the upstream tool declares its own parameter named responseCacheId/responseOffset/"
                "responseLimit, address the proxy field via the namespaced spelling (_proxy.responseOffset "
                "etc.), which is always reserved."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "toolName": {
                        "type": "string",
                        "description": (
                            "Exact toolName from discovery (`serverid__tool`, hex fallback, or legacy server/tool)."
                            if settings.safe_tool_names
                            else "Format: `<server-id>/<upstream-tool-name>`."
                        ),
                    },
                    "arguments": {
                        "type": "object",
                        "description": "JSON object of parameters for the upstream tool.",
                        "additionalProperties": True,
                    },
                    "responseCacheId": {
                        "type": "string",
                        "description": (
                            "From a previous paginated callTool response. Fetches the next slice without "
                            "re-invoking the upstream tool."
                        ),
                    },
                    "responseOffset": _schema_int(
                        description="Character offset into the cached response (default 0).",
                        minimum=0,
                    ),
                    "responseLimit": _schema_int(
                        description=(
                            "Optional page size in characters (capped by the server page limit)."
                        ),
                        minimum=1,
                    ),
                    "_proxy.responseCacheId": {
                        "type": "string",
                        "description": (
                            "Namespaced form of responseCacheId; always proxy-owned even when the "
                            "upstream tool declares a parameter of the same name."
                        ),
                    },
                    "_proxy.responseOffset": _schema_int(
                        description="Namespaced form of responseOffset.",
                        minimum=0,
                    ),
                    "_proxy.responseLimit": _schema_int(
                        description="Namespaced form of responseLimit.",
                        minimum=1,
                    ),
                },
                "required": ["toolName"],
            },
        ),
        mcp_types.Tool(
            name="htmlToPlainText",
            description=(
                "Extract readable plain text from an HTML string. Useful for HTML-only email bodies "
                "(call with body_html from imap_get_message when body_text is empty) or other HTML snippets."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "html": {
                        "type": "string",
                        "description": "HTML source to convert.",
                    },
                    "maxChars": _schema_int(
                        description="Optional maximum length (0 = no limit).",
                        minimum=0,
                        default=0,
                    ),
                },
                "required": ["html"],
            },
        ),
    ]


async def get_llm_preview_snapshot(
    store: ServerConfigStore,
    domain_store: DomainStore,
    settings: Settings | None = None,
    stats_store: ToolCallStatsStore | None = None,
    tool_cache: UpstreamToolListCache | None = None,
) -> dict[str, Any]:
    """Serializable view of what MCP clients receive for tools + instructions (admin preview)."""
    cfg = settings or Settings()
    ids = {d.id for d in domain_store.list_records()}
    if not ids:
        ids = {"default"}
    ids.add(_ADMIN_DOMAIN_ID)
    domain_ids = sorted(ids)
    if stats_store is not None:
        tools = await _build_session_tool_list(
            store, domain_ids, cfg, stats_store, tool_cache
        )
    else:
        tools = build_meta_tool_list(domain_ids, cfg)
    tool_dicts = [
        t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in tools
    ]
    resources_preview = [
        {
            "uri": _PROXY_DATETIME_RESOURCE_URI_STR,
            "name": "Current date and time",
            "description": (
                "Proxy host clock when read (UTC + unix; optional TZ-based local line). "
                "Not a substitute for the user’s local timezone unless TZ matches their environment."
            ),
            "mimeType": "text/plain",
        }
    ]
    return {
        "server": {
            "name": "mcp-proxy",
            "version": "0.1.0",
            "role": (
                "Aggregates upstream MCP servers; lists discovery meta-tools plus up to three popular composite "
                "shortcuts at session level."
            ),
        },
        "instructions": full_instructions_for_store(store, cfg.instructions_max_chars),
        "tools": tool_dicts,
        "resources": resources_preview,
        "extras": {
            "upstream_tools": (
                "Hidden until searchToolsForDomain / searchTool; searchToolsForDomain uses query + pagination "
                "(or listAll + pagination). Entries may include serverLlmContext when configured per server."
            ),
            "protocol": (
                "Full initialize/capabilities exchange is handled by the MCP SDK; "
                "this preview focuses on instructions + listed tools + built-in resources."
            ),
            "llm_context_limits": _llm_limits_excerpt(cfg),
        },
    }


def build_proxy_mcp_server(
    store: ServerConfigStore,
    domain_store: DomainStore,
    settings: Settings,
    stats_store: ToolCallStatsStore,
    *,
    live_tracker: LiveMcpTracker | None = None,
    tool_response_cache: ToolResponseCache | None = None,
    tool_list_cache: UpstreamToolListCache | None = None,
) -> Server:
    response_cache = tool_response_cache or ToolResponseCache()
    # PR 6.2: shared upstream tool-list TTL cache (app wires one from
    # settings so the admin API shares it with the MCP server).
    tool_cache = tool_list_cache or UpstreamToolListCache(
        ttl_s=settings.tool_list_cache_ttl_s
    )
    server = Server(
        "mcp-proxy",
        version="0.1.0",
        instructions=_instructions_for_mcp_request(store, settings),
    )

    def _domain_ids() -> list[str]:
        ids = {d.id for d in domain_store.list_records()}
        if not ids:
            ids = {"default"}
        ids.add(_ADMIN_DOMAIN_ID)
        return sorted(ids)

    @server.list_tools()
    async def list_tools() -> list[mcp_types.Tool]:
        return await _build_session_tool_list(
            store, _domain_ids(), settings, stats_store, tool_cache
        )

    @server.list_resources()
    async def list_resources() -> list[mcp_types.Resource]:
        return [
            mcp_types.Resource(
                uri=_PROXY_DATETIME_RESOURCE_URI_STR,
                name="Current date and time",
                description=(
                    "Snapshot of the MCP proxy host clock when read (UTC ISO 8601 and unix time). "
                    "Includes an optional local wall-clock line when the container has TZ set. "
                    "Refresh by calling resources/read again."
                ),
                mimeType="text/plain",
            )
        ]

    @server.read_resource()
    async def read_resource(uri: AnyUrl) -> Iterable[ReadResourceContents]:
        if str(uri).rstrip("/") != _PROXY_DATETIME_RESOURCE_URI_STR:
            raise McpError(
                mcp_types.ErrorData(
                    code=mcp_types.INVALID_PARAMS,
                    message=(
                        f"Unknown resource URI {str(uri)!r}. "
                        f"The proxy only defines {_PROXY_DATETIME_RESOURCE_URI_STR!r}."
                    ),
                )
            )
        return [
            ReadResourceContents(
                content=_proxy_datetime_resource_body(),
                mime_type="text/plain",
            )
        ]

    @server.call_tool()
    async def call_tool(
        name: str, arguments: dict | None
    ) -> list[mcp_types.ContentBlock]:
        args = arguments or {}
        name, args, hot_wire, _declared = await _as_hot_call_tool_args(
            name, args, store, settings, stats_store, tool_cache
        )
        display_tool = hot_wire or (
            str(args.get("toolName") or "callTool")
            if name == "callTool"
            else name
        )
        disabled = _disabled_for_request()
        if hot_wire is not None:
            assert_tool_allowed(hot_wire, disabled)
        async with live_tool_span(
            live_tracker,
            tool_name=display_tool,
            arguments=args if isinstance(args, dict) else None,
        ):
            return await _call_tool_impl(name, args)

    async def _call_tool_impl(
        name: str, arguments: dict | None
    ) -> list[mcp_types.ContentBlock]:
        eff = _effective_settings(settings)
        disabled = _disabled_for_request()
        args = arguments or {}
        name, args, hot_wire, declared_params = await _as_hot_call_tool_args(
            name, args, store, settings, stats_store, tool_cache
        )
        if hot_wire is not None:
            assert_tool_allowed(hot_wire, disabled)

        assert_tool_allowed(name, disabled)
        _assert_mcp_admin_allowed(name, settings)

        if name == "searchToolsForDomain":
            dom = args.get("domain")
            if not isinstance(dom, str) or not dom.strip():
                raise McpError(
                    mcp_types.ErrorData(
                        code=mcp_types.INVALID_PARAMS,
                        message="Missing or invalid 'domain' (string).",
                    )
                )
            dom = dom.strip()
            if dom not in set(_domain_ids()):
                raise McpError(
                    mcp_types.ErrorData(
                        code=mcp_types.INVALID_PARAMS,
                        message=f"Unknown domain {dom!r}. Known: {_domain_ids()}",
                    )
                )
            list_all = _coerce_bool_arg("listAll", args.get("listAll"))
            query_raw = args.get("query")
            query_ok = isinstance(query_raw, str) and bool(query_raw.strip())
            if list_all and query_ok:
                raise McpError(
                    mcp_types.ErrorData(
                        code=mcp_types.INVALID_PARAMS,
                        message=(
                            "Use either listAll=true (full catalog, paginated) or a non-empty query, not both. "
                            "Omit query when listing all tools."
                        ),
                    )
                )
            if not list_all and not query_ok:
                raise McpError(
                    mcp_types.ErrorData(
                        code=mcp_types.INVALID_PARAMS,
                        message=(
                            "Provide a non-empty query to search within the domain, or set listAll=true to "
                            "enumerate all tools with pagination (offset/limit, hasMore)."
                        ),
                    )
                )
            offset, page_limit = _parse_domain_pagination(args, eff)
            defs, degraded = await _collect_all_tool_defs(store, settings, dom, tool_cache)
            if list_all:
                ordered = sorted(defs, key=lambda r: str(r.get("toolName", "")).lower())
                total = len(ordered)
                page = ordered[offset : offset + page_limit]
                mode = "listAll"
            else:
                q = str(query_raw).strip().lower()
                matches = [d for d in defs if _tool_row_matches_query(d, q)]
                matches.sort(key=lambda m: _rank_match_key(m, q))
                total = len(matches)
                page = matches[offset : offset + page_limit]
                mode = "filtered"
            shaped = [_shape_tool_row_for_llm(d, eff) for d in page]
            returned = len(shaped)
            payload: dict[str, Any] = {
                "mode": mode,
                "domain": dom,
                "tools": shaped,
                # PR 5.6: servers whose tool list failed to load this round —
                # their tools are missing from the result, don't treat as absent.
                "degradedServers": degraded,
                "pagination": {
                    "offset": offset,
                    "limit": page_limit,
                    "returned": returned,
                    "total": total,
                    "hasMore": offset + returned < total,
                },
            }
            return [
                mcp_types.TextContent(
                    type="text",
                    text=_json_discovery(payload, eff),
                )
            ]

        if name == "searchTool":
            query = args.get("query")
            if not isinstance(query, str) or not query.strip():
                raise McpError(
                    mcp_types.ErrorData(
                        code=mcp_types.INVALID_PARAMS,
                        message="Missing or invalid 'query' (non-empty string).",
                    )
                )
            q = query.strip().lower()
            dom_filter: str | None = None
            if "domain" in args and args["domain"] is not None:
                if not isinstance(args["domain"], str) or not args["domain"].strip():
                    raise McpError(
                        mcp_types.ErrorData(
                            code=mcp_types.INVALID_PARAMS,
                            message="If provided, 'domain' must be a non-empty string.",
                        )
                    )
                dom_filter = args["domain"].strip()
                if dom_filter not in set(_domain_ids()):
                    raise McpError(
                        mcp_types.ErrorData(
                            code=mcp_types.INVALID_PARAMS,
                            message=f"Unknown domain {dom_filter!r}.",
                        )
                    )

            all_defs, degraded = await _collect_all_tool_defs(
                store, settings, dom_filter, tool_cache
            )
            matches = [d for d in all_defs if _tool_row_matches_query(d, q)]
            matches.sort(key=lambda m: _rank_match_key(m, q))
            total = len(matches)
            search_max = eff.tool_search_max_matches
            truncated = search_max > 0 and total > search_max
            if truncated:
                matches = matches[:search_max]
            shaped = [_shape_tool_row_for_llm(m, eff) for m in matches]
            out: list[mcp_types.ContentBlock] = [
                mcp_types.TextContent(
                    type="text",
                    text=_json_discovery(shaped, eff),
                )
            ]
            # PR 5.6: keep the healthy payload byte-identical (single array
            # block), but when information is missing say so — an extra block
            # with {truncated, total, degradedServers}. Concatenating clients
            # only see it in the rare degraded/overflow case.
            if degraded or truncated:
                out.append(
                    mcp_types.TextContent(
                        type="text",
                        text=_json_discovery(
                            {
                                "discoveryMeta": {
                                    "truncated": truncated,
                                    "total": total,
                                    "maxMatches": search_max,
                                    "degradedServers": degraded,
                                }
                            },
                            eff,
                        ),
                    )
                )
            return out

        if name == "htmlToPlainText":
            html_raw = args.get("html")
            if not isinstance(html_raw, str):
                raise McpError(
                    mcp_types.ErrorData(
                        code=mcp_types.INVALID_PARAMS,
                        message="Missing or invalid 'html' (string).",
                    )
                )
            max_raw = args.get("maxChars", 0)
            try:
                max_chars = int(max_raw) if max_raw is not None else 0
            except (TypeError, ValueError):
                raise McpError(
                    mcp_types.ErrorData(
                        code=mcp_types.INVALID_PARAMS,
                        message="'maxChars' must be a non-negative integer.",
                    )
                )
            if max_chars < 0:
                raise McpError(
                    mcp_types.ErrorData(
                        code=mcp_types.INVALID_PARAMS,
                        message="'maxChars' must be >= 0.",
                    )
                )
            plain, truncated = html_to_plain_text(html_raw, max_chars=max_chars)
            payload = {
                "plainText": plain,
                "truncated": truncated,
                "charCount": len(plain),
            }
            return [
                mcp_types.TextContent(
                    type="text",
                    text=_json_discovery(payload, eff),
                )
            ]

        if name == "callTool":
            tool_name = args.get("toolName")
            if not isinstance(tool_name, str) or not tool_name.strip():
                raise McpError(
                    mcp_types.ErrorData(
                        code=mcp_types.INVALID_PARAMS,
                        message="Missing or invalid 'toolName' (string).",
                    )
                )
            raw_tool_args = args.get("arguments")
            if raw_tool_args is not None and not isinstance(raw_tool_args, dict):
                raise McpError(
                    mcp_types.ErrorData(
                        code=mcp_types.INVALID_PARAMS,
                        message="'arguments' must be a JSON object when provided.",
                    )
                )
            tool_args, pagination = parse_call_tool_pagination(
                args, eff, declared_params=declared_params
            )
            composite_key = tool_name.strip()
            assert_tool_allowed(composite_key, disabled)
            if pagination.cache_id:
                entry = response_cache.get(pagination.cache_id)
                if entry is None:
                    raise McpError(
                        mcp_types.ErrorData(
                            code=mcp_types.INVALID_PARAMS,
                            message=(
                                "responseCacheId is missing or expired. Re-run callTool without "
                                "responseCacheId/responseOffset to fetch a fresh response."
                            ),
                        )
                    )
                return paginate_from_cache(
                    entry,
                    settings=eff,
                    cache_id=pagination.cache_id,
                    offset=pagination.offset,
                    limit=pagination.limit,
                    tool_name=composite_key,
                )
            sid, orig = _split_proxy_tool_name(composite_key)
            if sid == _ADMIN_SERVER_ID:
                out = await _call_tool_impl(orig, tool_args)
                stats_store.record_success(composite_key)
                return paginate_call_tool_response(
                    out,
                    settings=eff,
                    cache=response_cache,
                    tool_name=composite_key,
                    pagination=pagination,
                )
            upstream = store.get(sid)
            if upstream is None or not upstream.enabled:
                if stats_store.remove(composite_key):
                    log.info(
                        "Pruned popular tool shortcut %r (unknown or disabled server %r)",
                        composite_key,
                        sid,
                    )
                raise McpError(
                    mcp_types.ErrorData(
                        code=mcp_types.INVALID_PARAMS,
                        message=f"Unknown or disabled upstream server {sid!r}",
                    )
                )
            stderr_accum: list[str] = []
            try:
                t0 = time.monotonic()
                with anyio.fail_after(settings.upstream_timeout_s):
                    async with _upstream_streams(
                        upstream,
                        stdio_stderr_sink=stderr_accum,
                    ) as (
                        read_stream,
                        write_stream,
                    ):
                        async with ClientSession(
                            read_stream,
                            write_stream,
                            client_info=mcp_types.Implementation(
                                name="mcp-proxy", version="0.1.0"
                            ),
                        ) as session:
                            await session.initialize()
                            result = await session.call_tool(orig, tool_args)
            except McpError:
                raise
            except TimeoutError as e:
                elapsed = time.monotonic() - t0 if "t0" in locals() else None
                tail = stderr_accum[0].strip() if stderr_accum else ""
                msg = (
                    f"Upstream {sid!r} timed out "
                    f"(timeout_s={settings.upstream_timeout_s:g}"
                    + (f", elapsed_s={elapsed:.1f}" if elapsed is not None else "")
                    + ")"
                )
                if tail:
                    msg += (
                        "\n\n--- stderr (upstream subprocess, possibly partial) ---\n"
                        f"{tail[-8000:]}"
                    )
                raise McpError(
                    mcp_types.ErrorData(
                        code=mcp_types.INTERNAL_ERROR,
                        message=msg,
                    )
                ) from e
            except Exception as e:
                log.exception("callTool failed for %s", tool_name)
                stderr_txt = stderr_accum[0].strip() if stderr_accum else ""
                detail = format_upstream_stdio_error(e, stderr_txt)
                raise McpError(
                    mcp_types.ErrorData(
                        code=mcp_types.INTERNAL_ERROR,
                        message=detail or type(e).__name__,
                    )
                ) from e
            # An upstream tool error must not count as a successful call once
            # propagation is on (stats drive the popular-tool shortcut).
            if not (settings.propagate_tool_errors and result.isError):
                stats_store.record_success(composite_key)
            blocks = paginate_call_tool_response(
                list(result.content or []),
                settings=eff,
                cache=response_cache,
                tool_name=composite_key,
                pagination=pagination,
            )
            # PR 5.2a: with MCP_PROXY_PROPAGATE_TOOL_ERRORS=true keep the
            # upstream isError flag + structuredContent; default keeps today's
            # content-blocks-only behavior.
            return apply_upstream_error_semantics(
                result, blocks, settings=settings
            )

        if name == "listServers":
            rows: list[dict[str, Any]] = []
            for s in store.list_servers():
                row: dict[str, Any] = {
                    "id": s.id,
                    "domain": s.domain,
                    "type": s.type,
                    "enabled": s.enabled,
                    "displayName": s.display_name,
                    "target": (
                        s.url if s.type == "http" else (" ".join(s.command or []))
                    ),
                }
                if s.type == "stdio":
                    meta = get_stdio_meta(settings.data_dir, s.id)
                    if meta:
                        row["managed"] = True
                        row["ecosystem"] = meta.get("ecosystem")
                        row["packageSpec"] = meta.get("package_spec")
                    else:
                        row["managed"] = False
                rows.append(row)
            rows.sort(key=lambda r: str(r.get("id", "")))
            return [
                mcp_types.TextContent(type="text", text=_json_discovery(rows, settings))
            ]

        if name == "setServerEnabled":
            server_id = args.get("serverId")
            if not isinstance(server_id, str) or not server_id.strip():
                raise McpError(
                    mcp_types.ErrorData(
                        code=mcp_types.INVALID_PARAMS,
                        message="Missing or invalid 'serverId' (string).",
                    )
                )
            enabled = _coerce_bool_arg("enabled", args.get("enabled"))
            sid = validate_slug_id(server_id)
            try:
                store.update_fields(
                    sid, lambda srv: srv.model_copy(update={"enabled": enabled})
                )
            except KeyError:
                raise McpError(
                    mcp_types.ErrorData(
                        code=mcp_types.INVALID_PARAMS,
                        message=f"Unknown server id {sid!r}.",
                    )
                )
            payload = {"ok": True, "serverId": sid, "enabled": enabled}
            return [
                mcp_types.TextContent(
                    type="text", text=_json_discovery(payload, settings)
                )
            ]

        if name == "registerStdioServer":
            ecosystem = args.get("ecosystem")
            if ecosystem not in {"pypi", "npm"}:
                raise McpError(
                    mcp_types.ErrorData(
                        code=mcp_types.INVALID_PARAMS,
                        message="'ecosystem' must be 'pypi' or 'npm'.",
                    )
                )
            server_id_raw = args.get("serverId")
            domain_raw = args.get("domain")
            package_raw = args.get("package")
            if not isinstance(server_id_raw, str) or not server_id_raw.strip():
                raise McpError(
                    mcp_types.ErrorData(
                        code=mcp_types.INVALID_PARAMS,
                        message="Missing or invalid 'serverId'.",
                    )
                )
            if not isinstance(domain_raw, str) or not domain_raw.strip():
                raise McpError(
                    mcp_types.ErrorData(
                        code=mcp_types.INVALID_PARAMS,
                        message="Missing or invalid 'domain'.",
                    )
                )
            if not isinstance(package_raw, str) or not package_raw.strip():
                raise McpError(
                    mcp_types.ErrorData(
                        code=mcp_types.INVALID_PARAMS,
                        message="Missing or invalid 'package'.",
                    )
                )
            sid = validate_slug_id(server_id_raw)
            domain = validate_slug_id(domain_raw)
            package_spec = package_raw.strip()
            if domain not in domain_store.id_set():
                raise McpError(
                    mcp_types.ErrorData(
                        code=mcp_types.INVALID_PARAMS,
                        message=f"Unknown domain {domain!r}.",
                    )
                )
            if ecosystem == "pypi":
                if not settings.allow_pypi_install:
                    raise McpError(
                        mcp_types.ErrorData(
                            code=mcp_types.INVALID_PARAMS,
                            message="PyPI install is disabled (MCP_PROXY_ALLOW_PYPI_INSTALL is false).",
                        )
                    )
                validate_package_spec(package_spec)
                result = await _install_in_thread(
                    install_into_venv, settings.data_dir, sid, package_spec
                )
            else:
                if not settings.allow_npm_install:
                    raise McpError(
                        mcp_types.ErrorData(
                            code=mcp_types.INVALID_PARAMS,
                            message="npm install is disabled (MCP_PROXY_ALLOW_NPM_INSTALL is false).",
                        )
                    )
                validate_npm_package_spec(package_spec)
                result = await _install_in_thread(
                    install_npm_prefix, settings.data_dir, sid, package_spec
                )
            if not result.ok:
                payload = {
                    "ok": False,
                    "registered": False,
                    "detail": "Install command failed (see log).",
                    "log": result.log,
                }
                return [
                    mcp_types.TextContent(
                        type="text", text=_json_discovery(payload, settings)
                    )
                ]
            if ecosystem == "pypi":
                suggested_argv = (
                    [result.suggested_command] if result.suggested_command else None
                )
            else:
                suggested_argv = result.suggested_argv
            if not suggested_argv:
                payload = {
                    "ok": False,
                    "registered": False,
                    "detail": "Install succeeded but no CLI binary was detected.",
                    "log": result.log,
                }
                return [
                    mcp_types.TextContent(
                        type="text", text=_json_discovery(payload, settings)
                    )
                ]
            existing = store.get(sid)
            if existing is not None and existing.type != "stdio":
                raise McpError(
                    mcp_types.ErrorData(
                        code=mcp_types.INVALID_PARAMS,
                        message=f"server id {sid!r} already exists as HTTP.",
                    )
                )
            display_name = args.get("displayName")
            if display_name is not None:
                display_name = str(display_name).strip() or None
            llm_context = str(args.get("llmContext", ""))
            if "env" in args:
                env = _coerce_str_dict_arg("env", args.get("env"))
            elif existing is not None:
                env = dict(existing.env)
            else:
                env = {}
            server = UpstreamServer(
                id=sid,
                domain=domain,
                type="stdio",
                enabled=True,
                display_name=display_name,
                llm_context=llm_context,
                command=suggested_argv,
                cwd=None,
                env=env,
            )
            try:
                store.add(server)
            except ValueError:
                store.update(sid, server)
            # 6.2: package contents changed even if the command string did not.
            tool_cache.invalidate(sid)
            set_stdio_meta(settings.data_dir, sid, ecosystem, package_spec)
            payload = {
                "ok": True,
                "registered": True,
                "server": server.model_dump(mode="json"),
                "log": result.log,
            }
            return [
                mcp_types.TextContent(
                    type="text", text=_json_discovery(payload, settings)
                )
            ]

        if name == "registerManualStdioServer":
            server_id_raw = args.get("serverId")
            domain_raw = args.get("domain")
            command_raw = args.get("command")
            if not isinstance(server_id_raw, str) or not server_id_raw.strip():
                raise McpError(
                    mcp_types.ErrorData(
                        code=mcp_types.INVALID_PARAMS,
                        message="Missing or invalid 'serverId'.",
                    )
                )
            if not isinstance(domain_raw, str) or not domain_raw.strip():
                raise McpError(
                    mcp_types.ErrorData(
                        code=mcp_types.INVALID_PARAMS,
                        message="Missing or invalid 'domain'.",
                    )
                )
            if not isinstance(command_raw, str) or not command_raw.strip():
                raise McpError(
                    mcp_types.ErrorData(
                        code=mcp_types.INVALID_PARAMS,
                        message="Missing or invalid 'command'.",
                    )
                )
            sid = validate_slug_id(server_id_raw)
            domain = validate_slug_id(domain_raw)
            if domain not in domain_store.id_set():
                raise McpError(
                    mcp_types.ErrorData(
                        code=mcp_types.INVALID_PARAMS,
                        message=f"Unknown domain {domain!r}.",
                    )
                )
            argv = _split_command(command_raw)
            if not argv:
                raise McpError(
                    mcp_types.ErrorData(
                        code=mcp_types.INVALID_PARAMS,
                        message="'command' parsed to an empty argv.",
                    )
                )
            existing = store.get(sid)
            if existing is not None and existing.type != "stdio":
                raise McpError(
                    mcp_types.ErrorData(
                        code=mcp_types.INVALID_PARAMS,
                        message=(
                            f"server id {sid!r} already exists as HTTP; remove it with "
                            "removeServer before registering stdio."
                        ),
                    )
                )
            if "env" in args and args.get("env") is not None:
                env = _coerce_str_dict_arg("env", args.get("env"))
            elif existing is not None:
                env = dict(existing.env)
            else:
                env = {}
            display_name = args.get("displayName")
            if display_name is not None:
                display_name = str(display_name).strip() or None
            if (
                existing is not None
                and existing.type == "stdio"
                and "displayName" not in args
            ):
                display_name = existing.display_name
            if "llmContext" in args:
                llm_context = str(args.get("llmContext", ""))
            elif existing is not None and existing.type == "stdio":
                llm_context = existing.llm_context or ""
            else:
                llm_context = ""
            cwd_raw = args.get("cwd")
            cwd: str | None
            if cwd_raw is None or (isinstance(cwd_raw, str) and not cwd_raw.strip()):
                cwd = None
            elif isinstance(cwd_raw, str):
                cwd = cwd_raw.strip() or None
            else:
                raise McpError(
                    mcp_types.ErrorData(
                        code=mcp_types.INVALID_PARAMS,
                        message="'cwd' must be a string when provided.",
                    )
                )
            if (
                existing is not None
                and existing.type == "stdio"
                and "cwd" not in args
            ):
                cwd = existing.cwd
            if "enabled" in args:
                enabled = _coerce_bool_arg("enabled", args.get("enabled"))
            elif existing is not None:
                enabled = existing.enabled
            else:
                enabled = True
            try:
                server = UpstreamServer(
                    id=sid,
                    domain=domain,
                    type="stdio",
                    enabled=enabled,
                    display_name=display_name,
                    llm_context=llm_context,
                    command=argv,
                    cwd=cwd,
                    env=env,
                )
            except ValueError as e:
                raise McpError(
                    mcp_types.ErrorData(
                        code=mcp_types.INVALID_PARAMS,
                        message=str(e) or "Invalid server definition.",
                    )
                ) from e
            remove_stdio_meta(settings.data_dir, sid)
            try:
                store.add(server)
            except ValueError:
                store.update(sid, server)
            tool_cache.invalidate(sid)  # 6.2
            payload = {
                "ok": True,
                "registered": True,
                "upserted": existing is not None,
                "server": server.model_dump(mode="json"),
            }
            return [
                mcp_types.TextContent(
                    type="text", text=_json_discovery(payload, settings)
                )
            ]

        if name == "upgradeStdioServer":
            server_id = args.get("serverId")
            if not isinstance(server_id, str) or not server_id.strip():
                raise McpError(
                    mcp_types.ErrorData(
                        code=mcp_types.INVALID_PARAMS,
                        message="Missing or invalid 'serverId'.",
                    )
                )
            sid = validate_slug_id(server_id)
            srv = store.get(sid)
            if srv is None:
                raise McpError(
                    mcp_types.ErrorData(
                        code=mcp_types.INVALID_PARAMS,
                        message=f"Unknown server id {sid!r}.",
                    )
                )
            if srv.type != "stdio":
                raise McpError(
                    mcp_types.ErrorData(
                        code=mcp_types.INVALID_PARAMS,
                        message=f"Server {sid!r} is not stdio.",
                    )
                )
            meta = get_stdio_meta(settings.data_dir, sid)
            if not meta:
                raise McpError(
                    mcp_types.ErrorData(
                        code=mcp_types.INVALID_PARAMS,
                        message="No package metadata found for this server. Reinstall once to enable upgrades.",
                    )
                )
            ecosystem = meta["ecosystem"]
            upgrade_spec = (
                _npm_package_name(meta["package_spec"])
                if ecosystem == "npm"
                else _pypi_dist_from_spec(meta["package_spec"])
            )
            if ecosystem == "pypi":
                if not settings.allow_pypi_install:
                    raise McpError(
                        mcp_types.ErrorData(
                            code=mcp_types.INVALID_PARAMS,
                            message="PyPI install is disabled (MCP_PROXY_ALLOW_PYPI_INSTALL is false).",
                        )
                    )
                result = await _install_in_thread(
                    install_into_venv, settings.data_dir, sid, upgrade_spec
                )
            else:
                if not settings.allow_npm_install:
                    raise McpError(
                        mcp_types.ErrorData(
                            code=mcp_types.INVALID_PARAMS,
                            message="npm install is disabled (MCP_PROXY_ALLOW_NPM_INSTALL is false).",
                        )
                    )
                result = await _install_in_thread(
                    install_npm_prefix, settings.data_dir, sid, upgrade_spec
                )
            if not result.ok:
                payload = {
                    "ok": False,
                    "upgraded": False,
                    "detail": "Upgrade install failed.",
                    "log": result.log,
                }
                return [
                    mcp_types.TextContent(
                        type="text", text=_json_discovery(payload, settings)
                    )
                ]
            if ecosystem == "pypi":
                new_argv = (
                    [result.suggested_command] if result.suggested_command else None
                )
            else:
                new_argv = result.suggested_argv
            if new_argv:
                # CAS: replace only `command` on the *current* record — a
                # concurrent enable/edit during the (long) upgrade install
                # must not be rolled back by writing a pre-upgrade snapshot.
                try:
                    store.update_fields(
                        sid, lambda srv: srv.model_copy(update={"command": new_argv})
                    )
                except KeyError:
                    raise McpError(
                        mcp_types.ErrorData(
                            code=mcp_types.INVALID_PARAMS,
                            message=f"Server {sid!r} was removed during upgrade.",
                        )
                    )
            set_stdio_meta(settings.data_dir, sid, ecosystem, upgrade_spec)
            tool_cache.invalidate(sid)  # 6.2
            payload = {
                "ok": True,
                "upgraded": True,
                "serverId": sid,
                "packageSpec": upgrade_spec,
                "log": result.log,
            }
            return [
                mcp_types.TextContent(
                    type="text", text=_json_discovery(payload, settings)
                )
            ]

        if name == "removeServer":
            server_id = args.get("serverId")
            if not isinstance(server_id, str) or not server_id.strip():
                raise McpError(
                    mcp_types.ErrorData(
                        code=mcp_types.INVALID_PARAMS,
                        message="Missing or invalid 'serverId'.",
                    )
                )
            sid = validate_slug_id(server_id)
            removed = store.remove(sid)
            if not removed:
                raise McpError(
                    mcp_types.ErrorData(
                        code=mcp_types.INVALID_PARAMS,
                        message=f"Unknown server id {sid!r}.",
                    )
                )
            remove_stdio_meta(settings.data_dir, sid)
            tool_cache.invalidate(sid)  # 6.2
            payload = {"ok": True, "removed": True, "serverId": sid}
            return [
                mcp_types.TextContent(
                    type="text", text=_json_discovery(payload, settings)
                )
            ]

        if name == "getLogs":
            limit = args.get("limit", 200)
            if limit is None:
                limit = 200
            if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
                raise McpError(
                    mcp_types.ErrorData(
                        code=mcp_types.INVALID_PARAMS,
                        message="'limit' must be a positive integer.",
                    )
                )
            contains = args.get("contains")
            if contains is not None and not isinstance(contains, str):
                raise McpError(
                    mcp_types.ErrorData(
                        code=mcp_types.INVALID_PARAMS,
                        message="'contains' must be a string.",
                    )
                )
            lines = get_ring_handler().get_lines(min(limit, 1000))
            if contains:
                needle = contains.lower()
                lines = [ln for ln in lines if needle in ln.lower()]
            if not lines:
                text = "(no matching log lines)" if contains else "(log buffer empty)"
            else:
                text = "\n".join(lines)
            return [mcp_types.TextContent(type="text", text=text)]

        raise McpError(
            mcp_types.ErrorData(
                code=mcp_types.METHOD_NOT_FOUND,
                message=(
                    f"Unknown tool {name!r}. Use searchToolsForDomain, searchTool, callTool, htmlToPlainText "
                    "(or a listed popular composite shortcut), or admin tools such as listServers / setServerEnabled / "
                    "registerStdioServer / registerManualStdioServer / upgradeStdioServer / removeServer / getLogs."
                ),
            )
        )

    return server
