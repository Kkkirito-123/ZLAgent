"""Minimal FastMCP server used by ``mcp_e2e_check.py`` (stdio) and
``mcp_e2e_http_check.py`` (streamable-HTTP, v0.24.1).

Exposes two trivial tools — ``add`` and ``echo``. Spawned as a
subprocess by the e2e checks; not used in the offline smoke test.

Run directly to test the server in isolation::

    python scripts/mcp_mock_server.py
    python scripts/mcp_mock_server.py \
        --transport streamable-http --port 18765

The default (no args) is stdio; pass ``--transport streamable-http``
plus ``--port`` to bind on 127.0.0.1 for the HTTP e2e check.
"""
from __future__ import annotations

import argparse
import sys

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("zlagent-mock")


@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two integers and return their sum."""
    return a + b


@mcp.tool()
def echo(text: str) -> str:
    """Return the input text unchanged. Useful for round-trip tests."""
    return text


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ZLAgent mock MCP server.")
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default="stdio",
        help="MCP transport to expose (default: stdio).",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="HTTP host (only used with --transport streamable-http).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=18765,
        help="HTTP port (only used with --transport streamable-http).",
    )
    args = parser.parse_args()

    try:
        if args.transport == "streamable-http":
            # FastMCP's settings are mutable; override host/port before run().
            mcp.settings.host = args.host
            mcp.settings.port = args.port
            mcp.run(transport="streamable-http")
        else:
            mcp.run()
    except KeyboardInterrupt:
        sys.exit(0)
