"""Composite MCP tool-name encoding/decoding (shared by proxy core and policy).

Composite MCP tool names for upstream tools: legacy `server/tool`, or safe encoding for strict
clients (e.g. Cursor) using only [a-zA-Z0-9_]:

- Descriptive: `serverid__upstream_tool` (hyphens in server id → underscores)
- Fallback when the upstream name is not safely representable: `serverid__p__<hex utf-8 tool>`
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


def encode_proxy_tool_name(server_id: str, tool_name: str) -> str:
    sid = server_id.replace("-", "_")
    if SAFE_TOOL_TAIL.fullmatch(tool_name) and PROXY_TOOL_SEP not in tool_name:
        return f"{sid}__{tool_name}"
    hx = tool_name.encode("utf-8").hex()
    return f"{sid}{PROXY_TOOL_SEP}{hx}"


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
