"""End-to-end MCP **HTTP** integration check (v0.24.1).

Spawns ``mcp_mock_server.py`` as a streamable-HTTP server on a free
local port, drives it through :class:`MCPManager` via ``url=``,
calls ``add`` and ``echo``, asserts both round-trips work.

Companion to ``mcp_e2e_check.py`` (stdio). Operator-driven; spawns a real
subprocess and binds on a TCP port, so it is not part of any offline
smoke pass.

Run::

    python scripts/mcp_e2e_http_check.py
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import socket
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.mcp import MCPManager, MCPServerConfig
from backend.mcp.transport import configure_stderr_log_dir


def _find_free_port() -> int:
    """Bind on port 0, ask the OS for the assigned port, then release."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _wait_until_listening(host: str, port: int, timeout_seconds: float = 10.0) -> bool:
    """Poll ``host:port`` until something accepts a TCP connection."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


async def _run() -> int:
    configure_stderr_log_dir(ROOT / "workspace")

    server_script = pathlib.Path(__file__).parent / "mcp_mock_server.py"
    if not server_script.is_file():
        print(f"[FAIL] mock server missing at {server_script}", file=sys.stderr)
        return 2

    port = _find_free_port()
    proc = subprocess.Popen(
        [
            sys.executable, str(server_script),
            "--transport", "streamable-http",
            "--host", "127.0.0.1",
            "--port", str(port),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=dict(os.environ, PYTHONUNBUFFERED="1"),
    )

    try:
        if not _wait_until_listening("127.0.0.1", port):
            print(
                f"[FAIL] mock HTTP server did not listen on 127.0.0.1:{port}"
                f" within 10s",
                file=sys.stderr,
            )
            return 3

        url = f"http://127.0.0.1:{port}/mcp"
        cfg = MCPServerConfig(
            name="mock-http",
            url=url,
            connect_timeout_seconds=10.0,
            call_timeout_seconds=10.0,
        )
        manager = MCPManager({"mock-http": cfg})

        print(f"[..] starting MCPManager (http url={url})...")
        await manager.start()

        statuses = manager.status()
        if not statuses or not statuses[0].connected:
            err = statuses[0].error if statuses else "no status"
            print(f"[FAIL] mock HTTP server did not connect: {err}", file=sys.stderr)
            await manager.stop()
            return 4

        if statuses[0].transport != "http":
            print(
                f"[FAIL] expected transport='http', got {statuses[0].transport!r}",
                file=sys.stderr,
            )
            await manager.stop()
            return 5

        discovered = manager.list_tools()
        print(f"[OK] connected, {len(discovered)} tool(s) discovered:")
        for t in discovered:
            print(f"     - {t.name}: {t.description[:60]}")

        if not any(t.name == "add" for t in discovered):
            print("[FAIL] expected 'add' tool not found", file=sys.stderr)
            await manager.stop()
            return 6

        print("[..] calling add(7, 8)...")
        ok, content, err = await manager.call("mock-http", "add", {"a": 7, "b": 8})
        if not ok:
            print(f"[FAIL] add call failed: {err}", file=sys.stderr)
            await manager.stop()
            return 7
        print(f"[OK] add result content: {content!r}")
        if "15" not in content:
            print(
                f"[FAIL] expected '15' in response, got {content!r}",
                file=sys.stderr,
            )
            await manager.stop()
            return 8

        print("[..] calling echo('hello http mcp')...")
        ok, content, err = await manager.call("mock-http", "echo", {"text": "hello http mcp"})
        if not ok:
            print(f"[FAIL] echo call failed: {err}", file=sys.stderr)
            await manager.stop()
            return 9
        print(f"[OK] echo result content: {content!r}")
        if "hello http mcp" not in content:
            print(
                f"[FAIL] expected 'hello http mcp' in response, got {content!r}",
                file=sys.stderr,
            )
            await manager.stop()
            return 10

        print("[..] stopping manager...")
        await manager.stop()
        print("[OK] MCP HTTP e2e check passed")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
