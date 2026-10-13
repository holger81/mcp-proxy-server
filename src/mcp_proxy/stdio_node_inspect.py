"""Optional Node.js --inspect injection for stdio MCP upstream argv."""

from __future__ import annotations

import os
from pathlib import Path

from mcp_proxy.models import UpstreamServer

_INSPECT_NEEDLES = ("--inspect", "--inspect-brk")


def _inspect_host() -> str:
    """Bind address for the injected ``--inspect`` flag.

    Audit SEC-3: the DevTools protocol is unauthenticated (= arbitrary code
    execution). Bare-metal installs must not listen on every interface by
    default, so this is loopback unless the operator opts in via
    ``MCP_PROXY_NODE_INSPECT_HOST``. Inside Docker, port mappings reach the
    container's *routed* address (not its loopback), so the
    ``docker-compose.debug.yml`` override sets this to ``0.0.0.0`` while
    publishing only ``127.0.0.1:9229`` on the host.
    """
    return os.environ.get("MCP_PROXY_NODE_INSPECT_HOST", "127.0.0.1").strip() or "127.0.0.1"


def stdio_effective_command(server: UpstreamServer) -> list[str]:
    """Return argv used to spawn stdio MCP, inserting ``--inspect`` when configured."""
    cmd = list(server.command or [])
    if server.type != "stdio" or len(cmd) < 1:
        return cmd
    if not server.stdio_node_inspect:
        return cmd

    exe = Path(cmd[0]).name.lower()
    if exe not in ("node", "node.exe"):
        return cmd

    flattened = "\x00".join(cmd)
    if any(n in flattened for n in _INSPECT_NEEDLES):
        return cmd

    port = int(server.stdio_node_inspect_port)
    flag = "--inspect-brk" if server.stdio_node_inspect_brk else "--inspect"
    return [cmd[0], f"{flag}={_inspect_host()}:{port}", *cmd[1:]]
