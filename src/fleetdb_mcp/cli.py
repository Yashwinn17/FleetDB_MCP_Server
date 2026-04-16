"""CLI entry point — `fleetdb-mcp`.

Default: stdio transport (how MCP clients like Claude Desktop launch us).
Optional: --transport streamable-http --port 8000 for local debugging.
"""

from __future__ import annotations

import asyncio
import logging
import sys

import click

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from .server import mcp


def _configure_logging() -> None:
    # MCP stdio: NEVER log to stdout — that corrupts the JSON-RPC stream.
    # We log to stderr, which is free to use.
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


@click.command()
@click.option(
    "--transport",
    type=click.Choice(["stdio", "streamable-http", "sse"]),
    default="stdio",
    help="MCP transport. Default 'stdio' for local clients; 'streamable-http' for remote.",
)
@click.option(
    "--port",
    type=int,
    default=8000,
    show_default=True,
    help="Port for HTTP transports.",
)
def main(transport: str, port: int) -> None:
    """Launch the FleetDB MCP server."""
    _configure_logging()
    if transport == "stdio":
        if sys.stdin.isatty() and sys.stdout.isatty():
            raise click.ClickException(
                "The default stdio transport is meant to be launched by an MCP client, not typed into directly.\n"
                "Run `fleetdb-mcp --transport streamable-http --port 8000` for local manual testing,\n"
                "or configure your MCP client to launch `fleetdb-mcp` over stdio."
            )
        mcp.run()
    else:
        # FastMCP reads port from settings; set via env or use default.
        mcp.settings.port = port
        mcp.run(transport=transport)


if __name__ == "__main__":
    main()
