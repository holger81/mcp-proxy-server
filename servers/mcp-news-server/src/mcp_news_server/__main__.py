from __future__ import annotations

import asyncio
import logging
import sys

from mcp.server.stdio import stdio_server

from mcp_news_server.server import build_news_server

log = logging.getLogger("mcp_news_server")


async def _run() -> None:
    server = build_news_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main() -> None:
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="%(levelname)s %(name)s %(message)s")
    asyncio.run(_run())


if __name__ == "__main__":
    main()
