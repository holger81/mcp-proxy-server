"""Optional Node.js --inspect injection for stdio MCP upstream argv."""

from __future__ import annotations

from pathlib import Path

from mcp_proxy.models import UpstreamServer

_INSPECT_NEEDLES = ("--inspect", "--inspect-brk")


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
    return [cmd[0], f"{flag}=0.0.0.0:{port}", *cmd[1:]]
