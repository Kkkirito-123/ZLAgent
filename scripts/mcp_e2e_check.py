"""End-to-end MCP integration check.

Spawns a tiny in-tree FastMCP server (``mcp_mock_server.py`` next to this
script) as a subprocess, drives it through :class:`MCPManager`, calls the
``add`` tool, and asserts the round-trip works.

This is operator-driven and lives outside any offline smoke pass because
it spawns a real subprocess — slightly higher latency (typically ~2–3 s)
and requires the same Python interpreter to be on the PATH that the bot
itself uses.

Run::

    python scripts/mcp_e2e_check.py
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.mcp import MCPManager, MCPServerConfig
from backend.mcp.transport import configure_stderr_log_dir


async def _run() -> int:
    configure_stderr_log_dir(ROOT / "workspace")

    server_script = pathlib.Path(__file__).parent / "mcp_mock_server.py"
    if not server_script.is_file():
        print(f"[FAIL] mock server missing at {server_script}", file=sys.stderr)
        return 2

    cfg = MCPServerConfig(
        name="mock",
        command=sys.executable,
        args=(str(server_script),),
        connect_timeout_seconds=20.0,
        call_timeout_seconds=15.0,
    )
    manager = MCPManager({"mock": cfg})

    print("[..] starting MCPManager...")
    await manager.start()

    statuses = manager.status()
    if not statuses or not statuses[0].connected:
        print(f"[FAIL] mock server did not connect: {statuses[0].error if statuses else 'no status'}")
        await manager.stop()
        return 3

    discovered = manager.list_tools()
    print(f"[OK] connected, {len(discovered)} tool(s) discovered:")
    for t in discovered:
        print(f"     - {t.name}: {t.description[:60]}")

    if not any(t.name == "add" for t in discovered):
        print("[FAIL] expected 'add' tool not found", file=sys.stderr)
        await manager.stop()
        return 4

    print("[..] calling add(2, 3)...")
    ok, content, err = await manager.call("mock", "add", {"a": 2, "b": 3})
    if not ok:
        print(f"[FAIL] add call failed: {err}", file=sys.stderr)
        await manager.stop()
        return 5
    print(f"[OK] add result content: {content!r}")
    if "5" not in content:
        print(f"[FAIL] expected '5' in response, got {content!r}", file=sys.stderr)
        await manager.stop()
        return 6

    print("[..] calling echo('hello mcp')...")
    ok, content, err = await manager.call("mock", "echo", {"text": "hello mcp"})
    if not ok:
        print(f"[FAIL] echo call failed: {err}", file=sys.stderr)
        await manager.stop()
        return 7
    print(f"[OK] echo result content: {content!r}")
    if "hello mcp" not in content:
        print(f"[FAIL] expected 'hello mcp' in response, got {content!r}", file=sys.stderr)
        await manager.stop()
        return 8

    print("[..] stopping manager...")
    await manager.stop()
    print("[OK] MCP e2e check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
