"""Persistent stdio MCP client hosted behind a synchronous bootstrap facade."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import threading
from collections.abc import Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import timedelta
from enum import Enum
from pathlib import Path
from typing import Any

from .config import McpConfig, McpConfigurationError, McpServerConfig


class McpCallErrorCode(str, Enum):
    """Stable failure classes exposed to the MCP tool adapter."""

    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"


class McpCallError(RuntimeError):
    """Sanitized MCP call failure that never contains resolved credentials."""

    def __init__(
        self,
        code: McpCallErrorCode,
        *,
        server_id: str,
        tool_name: str,
        cause_type: str,
    ) -> None:
        self.code = code
        self.server_id = server_id
        self.tool_name = tool_name
        self.cause_type = cause_type
        super().__init__(
            f"MCP {code.value} for {server_id}/{tool_name} ({cause_type})"
        )


@dataclass(frozen=True, slots=True)
class McpToolDescriptor:
    """Approved remote tool metadata copied out of the SDK session."""

    server_id: str
    name: str
    description: str
    input_schema: dict[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_schema", dict(self.input_schema))


@dataclass(slots=True)
class _SdkBindings:
    client_session: Any
    server_parameters: Any
    stdio_client: Any


@dataclass(slots=True)
class _Connection:
    config: McpServerConfig
    session: Any
    close_event: asyncio.Event
    owner_task: asyncio.Task[Any]
    tools: dict[str, McpToolDescriptor] = field(default_factory=dict)


class LocalMcpClient:
    """Own local MCP subprocesses on a dedicated event-loop thread."""

    def __init__(
        self,
        config: McpConfig,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._config = config
        source_environment = os.environ if environ is None else environ
        self._server_environments = {
            server.id: server.resolve_environment(source_environment)
            for server in config.servers
        }
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._sdk: _SdkBindings | None = None
        self._connections: dict[str, _Connection] = {}
        self._descriptors: tuple[McpToolDescriptor, ...] = ()
        self._closed = False

    @property
    def descriptors(self) -> tuple[McpToolDescriptor, ...]:
        """Return the approved, connected tool set discovered at startup."""

        return self._descriptors

    @property
    def is_running(self) -> bool:
        """Return whether the dedicated owner loop is alive."""

        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> None:
        """Import the optional SDK, connect servers, and discover allowlisted tools."""

        if self._thread is not None:
            raise RuntimeError("MCP client is already started")
        if self._closed:
            raise RuntimeError("MCP client is closed")
        self._sdk = _load_sdk()
        thread = threading.Thread(
            target=self._run_loop,
            name="zlagent-mcp-loop",
            daemon=True,
        )
        self._thread = thread
        thread.start()
        if not self._ready.wait(timeout=5):
            self.close()
            raise McpConfigurationError("MCP event loop did not start")
        loop = self._require_loop()
        future = asyncio.run_coroutine_threadsafe(self._open_all(), loop)
        total_timeout = sum(
            server.startup_timeout_seconds + server.request_timeout_seconds
            for server in self._config.servers
        ) + 5
        try:
            self._descriptors = tuple(future.result(timeout=total_timeout))
        except Exception as exc:
            future.cancel()
            try:
                self.close()
            except RuntimeError:
                pass
            if isinstance(exc, McpConfigurationError):
                raise
            raise McpConfigurationError(
                f"MCP startup failed ({type(exc).__name__})"
            ) from exc

    async def call_tool(
        self,
        *,
        server_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        idempotency_key: str,
    ) -> Any:
        """Call one approved tool on the owning MCP event loop."""

        loop = self._require_loop()
        future = asyncio.run_coroutine_threadsafe(
            self._call_tool(
                server_id=server_id,
                tool_name=tool_name,
                arguments=dict(arguments),
                idempotency_key=idempotency_key,
            ),
            loop,
        )
        try:
            return await asyncio.wrap_future(future)
        except asyncio.CancelledError:
            future.cancel()
            raise
        except McpCallError:
            raise
        except TimeoutError as exc:
            future.cancel()
            raise McpCallError(
                McpCallErrorCode.TIMEOUT,
                server_id=server_id,
                tool_name=tool_name,
                cause_type=type(exc).__name__,
            ) from exc
        except Exception as exc:
            raise McpCallError(
                McpCallErrorCode.UNAVAILABLE,
                server_id=server_id,
                tool_name=tool_name,
                cause_type=type(exc).__name__,
            ) from exc

    def close(self) -> None:
        """Close sessions, terminate subprocesses, and stop the owner loop."""

        if self._closed:
            return
        self._closed = True
        loop = self._loop
        thread = self._thread
        close_error: Exception | None = None
        if loop is not None and loop.is_running():
            future = asyncio.run_coroutine_threadsafe(self._close_all(), loop)
            try:
                future.result(timeout=10)
            except Exception as exc:  # noqa: BLE001 - stop loop after close failure
                future.cancel()
                close_error = exc
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=10)
            if thread.is_alive() and close_error is None:
                close_error = RuntimeError("MCP event-loop thread did not stop")
        self._descriptors = ()
        self._server_environments.clear()
        if close_error is not None:
            raise RuntimeError(
                f"MCP shutdown failed ({type(close_error).__name__})"
            ) from close_error

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            loop.close()

    async def _open_all(self) -> list[McpToolDescriptor]:
        descriptors: list[McpToolDescriptor] = []
        try:
            for server in self._config.servers:
                connection = await self._open_server(server)
                self._connections[server.id] = connection
                descriptors.extend(connection.tools.values())
        except Exception:
            await self._close_all()
            raise
        return descriptors

    async def _open_server(self, server: McpServerConfig) -> _Connection:
        loop = asyncio.get_running_loop()
        ready: asyncio.Future[_Connection] = loop.create_future()
        close_event = asyncio.Event()
        owner_task = asyncio.create_task(
            self._own_server(server, ready=ready, close_event=close_event),
            name=f"zlagent-mcp-{server.id}",
        )
        try:
            return await ready
        except Exception:
            close_event.set()
            await asyncio.gather(owner_task, return_exceptions=True)
            raise

    async def _own_server(
        self,
        server: McpServerConfig,
        *,
        ready: asyncio.Future[_Connection],
        close_event: asyncio.Event,
    ) -> None:
        """Enter and exit each AnyIO transport context in one owner task."""

        assert self._sdk is not None
        stack = AsyncExitStack()
        errlog = _open_errlog()
        try:
            params = self._sdk.server_parameters(
                command=server.command,
                args=list(server.args),
                cwd=server.cwd,
                env=self._server_environments.pop(server.id, {}),
            )
            read_stream, write_stream = await stack.enter_async_context(
                self._sdk.stdio_client(params, errlog=errlog)
            )
            session = await stack.enter_async_context(
                self._sdk.client_session(
                    read_stream,
                    write_stream,
                    read_timeout_seconds=timedelta(
                        seconds=server.request_timeout_seconds
                    ),
                )
            )
            await asyncio.wait_for(
                session.initialize(),
                timeout=server.startup_timeout_seconds,
            )
            remote_tools = await asyncio.wait_for(
                self._list_tools(session, server),
                timeout=server.request_timeout_seconds,
            )
            missing = sorted(set(server.allowed_tools).difference(remote_tools))
            if missing:
                raise McpConfigurationError(
                    f"MCP server {server.id!r} did not expose allowlisted tools: "
                    + ", ".join(missing)
                )
            approved: dict[str, McpToolDescriptor] = {}
            for name in server.allowed_tools:
                remote_tool = remote_tools[name]
                description = (remote_tool.description or "").strip()
                input_schema = dict(remote_tool.inputSchema)
                if len(description) > 4_000:
                    raise McpConfigurationError(
                        f"MCP tool {server.id}/{name} description exceeds 4000 chars"
                    )
                schema_size = len(
                    json.dumps(input_schema, sort_keys=True, default=str)
                )
                if schema_size > 64_000:
                    raise McpConfigurationError(
                        f"MCP tool {server.id}/{name} input schema exceeds 64000 chars"
                    )
                approved[name] = McpToolDescriptor(
                    server_id=server.id,
                    name=name,
                    description=description,
                    input_schema=input_schema,
                )
            owner_task = asyncio.current_task()
            assert owner_task is not None
            connection = _Connection(
                config=server,
                session=session,
                close_event=close_event,
                owner_task=owner_task,
                tools=approved,
            )
            ready.set_result(connection)
            await close_event.wait()
        except Exception as exc:
            if ready.done():
                raise
            if isinstance(exc, McpConfigurationError):
                ready.set_exception(exc)
            else:
                ready.set_exception(
                    McpConfigurationError(
                        f"MCP server {server.id!r} failed to start "
                        f"({type(exc).__name__}); approved command: "
                        f"{server.display_command}"
                    )
                )
        finally:
            try:
                await stack.aclose()
            finally:
                errlog.close()

    async def _list_tools(
        self,
        session: Any,
        server: McpServerConfig,
    ) -> dict[str, Any]:
        tools: dict[str, Any] = {}
        cursor: str | None = None
        while True:
            result = await asyncio.wait_for(
                session.list_tools(cursor=cursor),
                timeout=server.request_timeout_seconds,
            )
            for tool in result.tools:
                if tool.name in tools:
                    raise McpConfigurationError(
                        f"MCP server {server.id!r} returned duplicate tool {tool.name!r}"
                    )
                tools[tool.name] = tool
                if len(tools) > 1_024:
                    raise McpConfigurationError(
                        f"MCP server {server.id!r} exposed more than 1024 tools"
                    )
            cursor = result.nextCursor
            if cursor is None:
                return tools

    async def _call_tool(
        self,
        *,
        server_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        idempotency_key: str,
    ) -> Any:
        connection = self._connections.get(server_id)
        if connection is None or tool_name not in connection.tools:
            raise McpCallError(
                McpCallErrorCode.UNAVAILABLE,
                server_id=server_id,
                tool_name=tool_name,
                cause_type="NotConnected",
            )
        try:
            return await asyncio.wait_for(
                connection.session.call_tool(
                    tool_name,
                    arguments,
                    read_timeout_seconds=timedelta(
                        seconds=connection.config.request_timeout_seconds
                    ),
                    meta={"io.zlagent/idempotencyKey": idempotency_key},
                ),
                timeout=connection.config.request_timeout_seconds,
            )
        except TimeoutError as exc:
            raise McpCallError(
                McpCallErrorCode.TIMEOUT,
                server_id=server_id,
                tool_name=tool_name,
                cause_type=type(exc).__name__,
            ) from exc
        except McpCallError:
            raise
        except Exception as exc:
            raise McpCallError(
                (
                    McpCallErrorCode.TIMEOUT
                    if _is_sdk_timeout(exc)
                    else McpCallErrorCode.UNAVAILABLE
                ),
                server_id=server_id,
                tool_name=tool_name,
                cause_type=type(exc).__name__,
            ) from exc

    async def _close_all(self) -> None:
        connections = list(self._connections.values())
        self._connections.clear()
        for connection in connections:
            connection.close_event.set()
        if connections:
            results = await asyncio.gather(
                *(connection.owner_task for connection in reversed(connections)),
                return_exceptions=True,
            )
            errors = [
                item
                for item in results
                if isinstance(item, Exception)
                and not _is_benign_shutdown_error(item)
            ]
            if errors:
                raise RuntimeError(
                    "one or more MCP server owner tasks failed during shutdown"
                ) from errors[0]

    def _require_loop(self) -> asyncio.AbstractEventLoop:
        loop = self._loop
        if self._closed or loop is None or not loop.is_running():
            raise McpCallError(
                McpCallErrorCode.UNAVAILABLE,
                server_id="unknown",
                tool_name="unknown",
                cause_type="ClientClosed",
            )
        return loop


def _load_sdk() -> _SdkBindings:
    try:
        mcp_module = importlib.import_module("mcp")
        stdio_module = importlib.import_module("mcp.client.stdio")
    except ModuleNotFoundError as exc:
        raise McpConfigurationError(
            "MCP support requires the optional dependency; "
            "install re-zlagent[mcp]"
        ) from exc
    return _SdkBindings(
        client_session=mcp_module.ClientSession,
        server_parameters=stdio_module.StdioServerParameters,
        stdio_client=stdio_module.stdio_client,
    )


def _open_errlog() -> Any:
    """Open the platform null sink outside Ruff's async blocking-call model."""

    return Path(os.devnull).open("w", encoding="utf-8")


def _is_benign_shutdown_error(error: BaseException) -> bool:
    """Recognize SDK stream closure races after a timed-out request."""

    if isinstance(error, BaseExceptionGroup):
        return all(_is_benign_shutdown_error(item) for item in error.exceptions)
    return type(error).__name__ in {
        "BrokenResourceError",
        "ClosedResourceError",
        "EndOfStream",
    }


def _is_sdk_timeout(error: Exception) -> bool:
    """Recognize the v1 SDK's McpError wrapper for request timeout code 408."""

    error_data = getattr(error, "error", None)
    code = getattr(error_data, "code", None)
    return isinstance(code, int) and not isinstance(code, bool) and code == 408
