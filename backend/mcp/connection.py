"""MCP server connection lifecycle.

A :class:`MCPServerConnection` owns one long-lived stdio subprocess and the
associated ``ClientSession``. The MCP SDK's stdio transport is implemented
as nested ``async with`` blocks — both must be entered and exited from the
same task or the underlying anyio cancel scopes will complain.

Our solution mirrors Hermes' approach but greatly simplified: we run a
single dedicated asyncio task per server (via ``asyncio.create_task``) that
opens the contexts, signals "ready" to the manager, then waits on a
shutdown event before unwinding. Because ZLAgent itself is async-first,
this task lives on the same loop as everything else — no thread bridging
needed.
"""
from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import Any, Optional

from loguru import logger
from mcp import ClientSession
from mcp.client.stdio import stdio_client

from .config import MCPServerConfig
from .credential import redact_for_log
from .http_transport import http_client_factory
from .transport import (
    build_stdio_params,
    get_stderr_log,
    sanitize_error,
    write_stderr_log_marker,
)


# -- Discovered tool descriptor ----------------------------------------------

@dataclass(slots=True)
class DiscoveredTool:
    """A tool advertised by an MCP server.

    Carries enough info to (a) register the tool in our :class:`ToolRegistry`
    and (b) call it back via :class:`MCPServerConnection.call_tool`. The raw
    JSON schema is preserved verbatim — we don't try to translate it because
    OpenAI / DeepSeek already accept the JSON Schema format MCP uses.
    """

    server_name: str
    name: str  # original tool name as advertised by the server
    description: str
    input_schema: dict[str, Any]


@dataclass(slots=True)
class _ConnectionState:
    """Mutable state shared between the connection task and the manager.

    ``ready_event`` is set after ``initialize()`` returns; if connection fails
    before that point, ``ready_error`` is populated and ``ready_event`` is
    set anyway so awaiting ``await self.wait_ready()`` always terminates.
    ``shutdown_event`` is what the connection task awaits before exiting its
    nested ``async with`` blocks.
    """

    ready_event: asyncio.Event = field(default_factory=asyncio.Event)
    shutdown_event: asyncio.Event = field(default_factory=asyncio.Event)
    ready_error: Optional[str] = None
    tools: tuple[DiscoveredTool, ...] = ()


# -- Connection ---------------------------------------------------------------

class MCPServerConnection:
    """One long-lived MCP stdio connection.

    Lifecycle::

        conn = MCPServerConnection(cfg)
        await conn.start()           # spawns the background task
        if conn.is_ready:
            tools = conn.tools
            result = await conn.call_tool("read_file", {"path": "..."})
        await conn.stop()

    The class is single-use: once ``stop()`` returns, you should construct a
    new one to reconnect. (Same convention as Hermes.)
    """

    def __init__(self, cfg: MCPServerConfig):
        self._cfg = cfg
        self._state = _ConnectionState()
        self._task: Optional[asyncio.Task[None]] = None
        # ``_call_lock`` serialises concurrent ``call_tool`` invocations on
        # the same session. The MCP SDK's session can in principle handle
        # interleaved requests, but most servers don't, and serialising keeps
        # the failure surface predictable.
        self._call_lock = asyncio.Lock()
        self._session: Optional[ClientSession] = None

    # -- public API ----------------------------------------------------------

    @property
    def name(self) -> str:
        return self._cfg.name

    @property
    def config(self) -> MCPServerConfig:
        return self._cfg

    @property
    def is_ready(self) -> bool:
        """True if ``initialize()`` succeeded and the session is usable."""
        return self._state.ready_event.is_set() and self._state.ready_error is None

    @property
    def ready_error(self) -> Optional[str]:
        return self._state.ready_error

    @property
    def tools(self) -> tuple[DiscoveredTool, ...]:
        return self._state.tools

    async def start(self) -> None:
        """Spawn the connection task and wait until it's ready (or has failed)."""
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name=f"mcp-{self._cfg.name}")
        try:
            await asyncio.wait_for(
                self._state.ready_event.wait(),
                timeout=self._cfg.connect_timeout_seconds + 5,
            )
        except asyncio.TimeoutError:
            self._state.ready_error = (
                f"connection task did not signal ready within"
                f" {self._cfg.connect_timeout_seconds + 5:.0f}s"
            )
            self._state.ready_event.set()
            await self.stop()

    async def stop(self) -> None:
        """Signal the connection task to exit and await its completion."""
        if self._task is None:
            return
        self._state.shutdown_event.set()
        try:
            await asyncio.wait_for(self._task, timeout=10.0)
        except asyncio.TimeoutError:
            logger.warning("[mcp] '{}' did not exit in 10s; cancelling", self._cfg.name)
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        finally:
            self._task = None
            self._session = None

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None,
    ) -> tuple[bool, str, Optional[str]]:
        """Invoke an MCP tool and flatten the result.

        Returns ``(ok, content, error)``:

        * ``ok=True``  → ``content`` is the textual concatenation of all
          ``TextContent`` blocks; ``error`` is ``None``.
        * ``ok=False`` → ``content`` may still hold any partial text the
          server returned; ``error`` carries a human-readable reason.

        Image/blob content blocks are stringified to a placeholder so the
        agent at least knows the server returned something non-textual.
        """
        if not self.is_ready or self._session is None:
            return False, "", f"server '{self._cfg.name}' is not connected"

        try:
            async with self._call_lock:
                result = await asyncio.wait_for(
                    self._session.call_tool(tool_name, arguments or {}),
                    timeout=self._cfg.call_timeout_seconds,
                )
        except asyncio.TimeoutError:
            return False, "", (
                f"call to '{self._cfg.name}/{tool_name}' timed out after"
                f" {self._cfg.call_timeout_seconds:.0f}s"
            )
        except Exception as exc:  # noqa: BLE001 — keep agent loop alive
            sanitized = sanitize_error(str(exc))
            logger.warning("[mcp] '{}/{}' raised: {}", self._cfg.name, tool_name, sanitized)
            return False, "", f"{type(exc).__name__}: {sanitized}"

        # Result content is a list of content blocks. We concatenate the
        # text ones — that's what the LLM gets back as the tool message.
        text_chunks: list[str] = []
        non_text_count = 0
        content = getattr(result, "content", None) or []
        for block in content:
            block_type = getattr(block, "type", None)
            if block_type == "text":
                text_chunks.append(getattr(block, "text", "") or "")
            else:
                non_text_count += 1
                # Render a placeholder so the LLM sees that something
                # came back even if it's binary.
                text_chunks.append(f"[{block_type or 'unknown'} content omitted]")

        body = "".join(text_chunks)
        is_error = bool(getattr(result, "isError", False))
        if is_error:
            return False, body, sanitize_error(body or "tool reported isError=true")
        if non_text_count and not body.strip():
            body = f"[{non_text_count} non-text content block(s) omitted]"
        return True, body, None

    async def wait_ready(self) -> bool:
        await self._state.ready_event.wait()
        return self.is_ready

    # -- internal ------------------------------------------------------------

    async def _drive_session(self, session: ClientSession) -> None:
        """``initialize`` → ``list_tools`` → mark ready → wait shutdown.

        Caller owns the outer transport context manager (stdio_client OR
        streamablehttp_client). Side-effects: populates ``_state.ready_*``
        and ``_state.tools``; awaits ``shutdown_event`` so the outer
        ``async with`` unwinds only when :meth:`stop` is called.
        """
        cfg = self._cfg
        self._session = session
        try:
            await asyncio.wait_for(
                session.initialize(),
                timeout=cfg.connect_timeout_seconds,
            )
        except asyncio.TimeoutError:
            self._state.ready_error = (
                f"initialize() timed out after"
                f" {cfg.connect_timeout_seconds:.0f}s"
            )
            self._state.ready_event.set()
            return

        # Discovery: pull the tool list before we mark ready
        try:
            tools_resp = await asyncio.wait_for(
                session.list_tools(),
                timeout=cfg.connect_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            self._state.ready_error = (
                f"list_tools() failed: {sanitize_error(str(exc))}"
            )
            self._state.ready_event.set()
            return

        discovered: list[DiscoveredTool] = []
        for t in tools_resp.tools:
            try:
                schema = t.inputSchema or {"type": "object", "properties": {}}
                if not isinstance(schema, dict):
                    schema = {"type": "object", "properties": {}}
                discovered.append(
                    DiscoveredTool(
                        server_name=cfg.name,
                        name=str(t.name),
                        description=str(t.description or ""),
                        input_schema=schema,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[mcp] '{}': skipping malformed tool {!r}: {}",
                    cfg.name, getattr(t, "name", "?"), exc,
                )
        self._state.tools = tuple(discovered)
        logger.info(
            "[mcp] '{}' connected ({}): {} tool(s) discovered",
            cfg.name, cfg.transport, len(discovered),
        )

        self._state.ready_event.set()
        # Wait until the manager tells us to shut down. The outer
        # transport ``async with`` blocks unwind cleanly here.
        await self._state.shutdown_event.wait()

    async def _run(self) -> None:
        """Connection task body. Picks transport then drives the session."""
        cfg = self._cfg

        try:
            if cfg.transport == "http":
                # streamable-HTTP transport. The factory probe
                # is process-cached; checking it inside the connection
                # task means a missing optional dependency surfaces as a
                # clean ready_error instead of crashing app boot.
                factory = http_client_factory()
                if factory is None:
                    self._state.ready_error = (
                        "streamable_http transport unavailable —"
                        " install the optional 'mcp[streamable-http]' extra"
                    )
                    return
                async with factory(
                    cfg.url,
                    headers=cfg.headers or None,
                    timeout=cfg.connect_timeout_seconds,
                ) as (read_stream, write_stream, _get_session_id):
                    async with ClientSession(read_stream, write_stream) as session:
                        await self._drive_session(session)
            else:
                params = build_stdio_params(cfg)
                write_stderr_log_marker(cfg.name)
                errlog = get_stderr_log()
                async with stdio_client(params, errlog=errlog) as (read_stream, write_stream):
                    async with ClientSession(read_stream, write_stream) as session:
                        await self._drive_session(session)

        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            err = sanitize_error(str(exc))
            # URLs may carry user:pass / ?token= — scrub before persisting.
            log_err = redact_for_log(err) or err
            self._state.ready_error = f"{type(exc).__name__}: {log_err}"
            self._state.ready_event.set()
            logger.warning(
                "[mcp] '{}' connection failed ({}): {}",
                cfg.name, cfg.transport, log_err,
            )
        finally:
            self._session = None
            # Make sure callers waiting on ready don't deadlock if we exited
            # without ever signalling ready.
            if not self._state.ready_event.is_set():
                self._state.ready_event.set()
