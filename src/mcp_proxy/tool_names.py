"""Composite MCP tool-name encoding/decoding (shared by proxy core and policy).

Composite MCP tool names for upstream tools: legacy `server/tool`, or safe encoding for strict
clients (e.g. Cursor) using only [a-zA-Z0-9_]:

- Descriptive: `serverid__upstream_tool` (hyphens in server id → underscores)
- Fallback when the upstream name is not safely representable: `serverid__p__<hex utf-8 tool>`
- Last resort for degenerate names even the hex form cannot represent (e.g. an
  empty tool name on a server id whose underscore form ends in `__p`): legacy
  `serverid/tool` — unambiguous because server ids can never contain `/`
  (PR 39 self-collision cleanup)

The encoder picks the first candidate that round-trips through
``decode_proxy_tool_name`` back to the exact ``(server_id, tool_name)`` pair;
the decoder itself is unchanged so wire names and stored policy keys from
before this fix keep parsing identically.
"""

from __future__ import annotations

import re

PROXY_TOOL_SEP = "__p__"
SAFE_TOOL_TAIL = re.compile(r"^[A-Za-z0-9_]+$")


def _hex_utf8_suffix_ok(s: str) -> bool:
    if len(s) % 2 != 0:
        return False
    if not s:
        return True
    if not all(ch in "0123456789abcdefABCDEF" for ch in s):
        return False
    try:
        bytes.fromhex(s).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    return True


def _decodes_exactly(wire: str, server_id: str, tool_name: str) -> bool:
    try:
        return decode_proxy_tool_name(wire) == (server_id, tool_name)
    except ValueError:
        return False


def encode_proxy_tool_name(server_id: str, tool_name: str) -> str:
    sid = server_id.replace("-", "_")
    if SAFE_TOOL_TAIL.fullmatch(tool_name) and PROXY_TOOL_SEP not in tool_name:
        wire = f"{sid}__{tool_name}"
        if _decodes_exactly(wire, server_id, tool_name):
            return wire
    hx = tool_name.encode("utf-8").hex()
    wire = f"{sid}{PROXY_TOOL_SEP}{hx}"
    if _decodes_exactly(wire, server_id, tool_name):
        return wire
    # The candidate forms would decode to a different (server, tool) pair —
    # e.g. an empty tool name on a sid whose underscore form ends in the
    # marker, where the hex branch reads the empty payload back for another
    # server. The legacy "/" split happens before any "__p__" scanning and
    # server ids can't contain "/", so this form round-trips for non-empty
    # tool names. Empty tool names cannot use "/" (decoder rejects empty
    # segments) — raise rather than emit an undecodable wire.
    legacy = f"{server_id}/{tool_name}"
    if _decodes_exactly(legacy, server_id, tool_name):
        return legacy
    raise ValueError(
        f"cannot encode proxy tool name for server_id={server_id!r} "
        f"tool_name={tool_name!r} without a round-tripping wire form"
    )


def decode_proxy_tool_name(composite: str) -> tuple[str, str]:
    c = composite.strip()
    if "/" in c:
        sid, tool = c.split("/", 1)
        sid, tool = sid.strip(), tool.strip()
        if not sid or not tool:
            raise ValueError("empty segment")
        return sid, tool
    i = 0
    while True:
        j = c.find(PROXY_TOOL_SEP, i)
        if j == -1:
            break
        left = c[:j]
        right = c[j + len(PROXY_TOOL_SEP) :]
        if left and _hex_utf8_suffix_ok(right):
            try:
                tool = bytes.fromhex(right).decode("utf-8") if right else ""
            except (ValueError, UnicodeDecodeError) as e:
                raise ValueError(str(e)) from e
            return left.replace("_", "-"), tool
        i = j + 1
    if "__" in c:
        left, right = c.split("__", 1)
        if left and right:
            return left.replace("_", "-"), right
    raise ValueError("not a composite tool name")


def canonical_tool_key(wire_name: str) -> str:
    """Map every spelling of a composite tool name to one canonical string.

    ``srv/tool``, ``srv__tool`` and ``srv__p__<hex>`` all decode to the same
    (server, tool) pair and therefore to the same key; names that are not
    composite (meta tools like ``callTool``) map to themselves. Used to
    compare against per-client ``disabled_tools`` entries so one disabled
    entry blocks every spelling (PLAN 4.1).
    """
    try:
        sid, tool = decode_proxy_tool_name(wire_name)
    except ValueError:
        return wire_name
    return encode_proxy_tool_name(sid, tool)
