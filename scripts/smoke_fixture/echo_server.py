"""Tiny stdio MCP server used by scripts/smoke_test.py.

Serves two tools so the proxy's discovery/execution path is exercised with
real MCP traffic: `echo` (round-trips text) and `boom` (raises, so FastMCP
returns an isError tool result — reserved for error-propagation assertions
in later PRs; the smoke script only asserts the happy path today).
"""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("smoke-echo")


@mcp.tool()
def echo(text: str) -> str:
    """Return the input text prefixed with 'echo:'."""
    return f"echo:{text}"


@mcp.tool()
def boom() -> str:
    """Always fails; used to assert error propagation."""
    raise ValueError("boom")


if __name__ == "__main__":
    mcp.run(transport="stdio")
