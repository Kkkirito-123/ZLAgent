"""Multi-server MCP coordinator with circuit breaker.

The manager owns one :class:`MCPServerConnection` per enabled config entry,
exposes a ``call_tool`` interface that routes by ``(server_name, tool_name)``,
and implements a simple consecutive-failure circuit breaker so a misbehaving
server can't keep timing out at the agent loop's expense.

Lifecycle from the FastAPI lifespan:

    manager = MCPManager(load_mcp_config(path))
    await manager.start()                     # connects everything in parallel
    discovered = manager.list_tools()         # for ToolRegistry registration
    ...
    await manager.stop()                      # signals every connection to exit

The breaker is per-server and very small: 5 consecutive failures opens it
for 60 seconds, during which calls return a synthetic error fast. Any
success closes it.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from loguru import logger

from .config import MCPServerConfig
from .connection import DiscoveredTool, MCPServerConnection
from .http_transport import http_client_factory


# Tunables: the defaults match Hermes' tools/mcp_tool.py constants.
DEFAULT_BREAKER_THRESHOLD = 5
DEFAULT_BREAKER_COOLDOWN_SECONDS = 60.0


@dataclass(slots=True)
class _BreakerState:
    """Per-server failure tracker."""

    consecutive_failures: int = 0
    opened_at: Optional[float] = None  # monotonic timestamp

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.opened_at = None

    def record_failure(self, threshold: int) -> None:
        self.consecutive_failures += 1
        if self.consecutive_failures >= threshold and self.opened_at is None:
            self.opened_at = time.monotonic()

    def is_open(self, cooldown: float) -> bool:
        if self.opened_at is None:
            return False
        if time.monotonic() - self.opened_at >= cooldown:
            # Half-open: let one call through. Reset the timer; if it fails
            # we'll open again immediately. Simpler than a real half-open
            # state machine and good enough for our scale.
            self.opened_at = None
            self.consecutive_failures = max(0, self.consecutive_failures - 1)
            return False
        return True


@dataclass(slots=True)
class ServerStatus:
    """Snapshot for the REST API."""

    name: str
    enabled: bool
    connected: bool
    error: Optional[str]
    tool_count: int
    breaker_open: bool
    consecutive_failures: int
    description: str
    # transport surface so /api/mcp can show stdio vs http
    # entries side by side. Default "stdio" preserves pre-v0.24 shape.
    transport: str = "stdio"


class MCPManager:
    """Top-level MCP coordinator.

    Conservative defaults: connection failures during ``start()`` are logged
    but do not raise — a single bad server entry should not prevent the rest
    of the FastAPI app from booting.
    """

    def __init__(
        self,
        configs: dict[str, MCPServerConfig],
        *,
        breaker_threshold: int = DEFAULT_BREAKER_THRESHOLD,
        breaker_cooldown_seconds: float = DEFAULT_BREAKER_COOLDOWN_SECONDS,
    ) -> None:
        self._configs = dict(configs)
        self._connections: dict[str, MCPServerConnection] = {}
        self._breakers: dict[str, _BreakerState] = {
            name: _BreakerState() for name in configs
        }
        self._breaker_threshold = breaker_threshold
        self._breaker_cooldown = float(breaker_cooldown_seconds)
        self._started = False

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Connect to every enabled server in parallel."""
        if self._started:
            return
        self._started = True

        tasks: list[asyncio.Task[None]] = []
        # `_http_pending` now only tracks HTTP servers whose
        # streamable_http SDK extra is missing. Reachability problems
        # (DNS, refused, 401, etc.) surface as MCPServerConnection's
        # `ready_error` like the stdio path, keeping the failure model
        # uniform across transports.
        self._http_pending: dict[str, str] = {}
        # Probe the HTTP factory once per start() so we don't pay the
        # import cost per-server. http_client_factory itself caches.
        http_factory_available = http_client_factory() is not None
        for name, cfg in self._configs.items():
            if not cfg.enabled:
                logger.info("[mcp] '{}' disabled in config; skipping", name)
                continue
            if cfg.transport == "http" and not http_factory_available:
                self._http_pending[name] = (
                    "streamable_http transport unavailable —"
                    " install the optional 'mcp[streamable-http]' extra"
                )
                logger.warning(
                    "[mcp] '{}' http transport requested but SDK extra missing; skipping",
                    name,
                )
                continue
            conn = MCPServerConnection(cfg)
            self._connections[name] = conn
            tasks.append(asyncio.create_task(conn.start(), name=f"mcp-start-{name}"))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        ready = sum(1 for c in self._connections.values() if c.is_ready)
        total = len(self._connections)
        logger.info("[mcp] manager started: {}/{} servers ready", ready, total)
        for name, conn in self._connections.items():
            if not conn.is_ready:
                logger.warning(
                    "[mcp] '{}' did not become ready: {}",
                    name, conn.ready_error or "unknown",
                )

    async def stop(self) -> None:
        """Tear down every connection in parallel."""
        if not self._started:
            return
        self._started = False
        tasks = [
            asyncio.create_task(conn.stop(), name=f"mcp-stop-{name}")
            for name, conn in self._connections.items()
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._connections.clear()
        logger.info("[mcp] manager stopped")

    async def add_server(
        self,
        cfg: MCPServerConfig,
    ) -> tuple[bool, Optional[str], tuple[Any, ...]]:
        """Attach a new MCP server at runtime (v0.33).

        This is the dynamic counterpart to ``start()``: where ``start()``
        bulk-connects every server pre-declared in YAML, ``add_server``
        spins up exactly one new connection on demand. The manager's
        internal dicts (``_configs``, ``_breakers``, ``_connections``,
        ``_http_pending``) are kept consistent so subsequent ``status()``,
        ``list_tools()``, and ``call()`` calls Just Work.

        Returns a triple ``(ok, error, discovered_tools)``:

        * ``ok=True``  — server is registered and (if ``cfg.enabled``)
                         connected and ready. ``discovered_tools`` is the
                         tuple of :class:`DiscoveredTool` the server
                         advertised, which the caller (typically
                         :class:`MCPManageTool`) feeds into ``ToolRegistry``.
        * ``ok=False`` — registration was rejected or the connection failed
                         to come up. ``error`` carries a human-readable
                         reason; the manager is left in a clean state
                         (no half-registered config, no orphan breaker).

        Idempotence: re-registering an existing name returns
        ``ok=False`` with a clear "already registered" message. Operators
        should use ``reconnect`` for bouncing or ``remove_server`` first
        for replacement.
        """
        name = cfg.name
        if name in self._configs:
            return False, f"server '{name}' is already registered", ()

        # Provisionally record state so a partial failure can roll back
        # cleanly. We delete on the failure paths below.
        self._configs[name] = cfg
        self._breakers[name] = _BreakerState()

        # Disabled-on-arrival is a valid config (operator wants the row
        # persisted but no live connection yet — e.g. credentials pending).
        if not cfg.enabled:
            return True, None, ()

        # HTTP without the SDK extra: behave like start()'s skip path.
        if cfg.transport == "http":
            if not hasattr(self, "_http_pending"):
                self._http_pending = {}
            if http_client_factory() is None:
                reason = (
                    "streamable_http transport unavailable — install the"
                    " optional 'mcp[streamable-http]' extra"
                )
                self._http_pending[name] = reason
                # Keep the config registered (so /api/mcp shows it) but
                # do not spin a connection. Ready=False.
                return False, reason, ()

        conn = MCPServerConnection(cfg)
        self._connections[name] = conn
        try:
            await conn.start()
        except Exception as exc:  # noqa: BLE001 — keep manager state coherent
            await self._rollback_failed_attach(name)
            return False, f"{type(exc).__name__}: {exc}", ()

        if not conn.is_ready:
            err = conn.ready_error or "connection failed"
            await self._rollback_failed_attach(name)
            return False, err, ()

        logger.info(
            "[mcp] add_server '{}' ready ({} tool(s) discovered)",
            name, len(conn.tools),
        )
        return True, None, tuple(conn.tools)

    async def remove_server(self, name: str) -> bool:
        """Detach a runtime-attached server (v0.33).

        Tears down the live connection if any, drops the breaker, and
        forgets the config. Returns False for unknown names; True after
        every successful removal. The caller (``mcp_manage`` tool) is
        responsible for unregistering tool wrappers from the
        :class:`ToolRegistry` because the manager intentionally has no
        knowledge of that subsystem.
        """
        if name not in self._configs:
            return False
        conn = self._connections.pop(name, None)
        if conn is not None:
            try:
                await conn.stop()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[mcp] remove_server: stop() raised for '{}': {}",
                    name, exc,
                )
        self._configs.pop(name, None)
        self._breakers.pop(name, None)
        if hasattr(self, "_http_pending"):
            self._http_pending.pop(name, None)
        logger.info("[mcp] remove_server '{}' done", name)
        return True

    async def _rollback_failed_attach(self, name: str) -> None:
        """Best-effort cleanup when ``add_server`` fails halfway through."""
        conn = self._connections.pop(name, None)
        if conn is not None:
            try:
                await conn.stop()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[mcp] rollback: stop() raised for '{}': {}", name, exc,
                )
        self._configs.pop(name, None)
        self._breakers.pop(name, None)
        if hasattr(self, "_http_pending"):
            self._http_pending.pop(name, None)

    async def reconnect(self, name: str) -> bool:
        """Tear down and recreate one server's connection.

        Returns True if the new connection became ready. Callers (REST handler)
        decide whether to surface the error.
        """
        cfg = self._configs.get(name)
        if cfg is None:
            return False
        old = self._connections.pop(name, None)
        if old is not None:
            await old.stop()
        # Reset breaker on operator-initiated reconnect.
        self._breakers[name] = _BreakerState()
        # Clear stale "SDK extra missing" record — operator may have just
        # installed the optional dependency; the new connection will
        # re-record it via its own ready_error if the probe still fails.
        if hasattr(self, "_http_pending"):
            self._http_pending.pop(name, None)
        if not cfg.enabled:
            return False
        conn = MCPServerConnection(cfg)
        self._connections[name] = conn
        await conn.start()
        return conn.is_ready

    # -- discovery -----------------------------------------------------------

    def list_tools(self) -> list[DiscoveredTool]:
        """Flatten tool lists across every ready server.

        Tools are filtered by the per-server include / exclude config here, so
        callers don't need to know about that policy.
        """
        out: list[DiscoveredTool] = []
        for name, conn in self._connections.items():
            cfg = self._configs[name]
            if not conn.is_ready:
                continue
            for tool in conn.tools:
                if not cfg.is_tool_allowed(tool.name):
                    continue
                out.append(tool)
        return out

    def get_connection(self, name: str) -> Optional[MCPServerConnection]:
        return self._connections.get(name)

    def get_config(self, name: str) -> Optional[MCPServerConfig]:
        return self._configs.get(name)

    def server_names(self) -> list[str]:
        return list(self._configs.keys())

    # -- status --------------------------------------------------------------

    def status(self) -> list[ServerStatus]:
        """Return a sortable list of server snapshots for the REST surface."""
        out: list[ServerStatus] = []
        http_pending = getattr(self, "_http_pending", {})
        for name, cfg in self._configs.items():
            conn = self._connections.get(name)
            breaker = self._breakers.get(name) or _BreakerState()
            connected = bool(conn and conn.is_ready)
            error = (conn.ready_error if conn else None) if not connected else None
            tool_count = len(conn.tools) if conn else 0
            # http servers don't take the stdio connection path;
            # surface the deferred-wire reason as ``error`` so /api/mcp
            # callers see why nothing is connected.
            if cfg.transport == "http" and name in http_pending:
                error = http_pending[name]
            out.append(
                ServerStatus(
                    name=name,
                    enabled=cfg.enabled,
                    connected=connected,
                    error=error,
                    tool_count=tool_count,
                    breaker_open=breaker.is_open(self._breaker_cooldown),
                    consecutive_failures=breaker.consecutive_failures,
                    description=cfg.description,
                    transport=cfg.transport,
                )
            )
        return out

    # -- call routing --------------------------------------------------------

    async def call(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any] | None,
    ) -> tuple[bool, str, Optional[str]]:
        """Route a tool call through the breaker into the per-server connection."""
        conn = self._connections.get(server_name)
        if conn is None:
            return False, "", f"unknown MCP server '{server_name}'"
        breaker = self._breakers.setdefault(server_name, _BreakerState())
        if breaker.is_open(self._breaker_cooldown):
            return False, "", (
                f"MCP server '{server_name}' is in circuit-breaker cooldown"
                f" after {breaker.consecutive_failures} consecutive failures;"
                f" wait {self._breaker_cooldown:.0f}s and retry"
            )
        ok, content, err = await conn.call_tool(tool_name, arguments)
        if ok:
            breaker.record_success()
        else:
            breaker.record_failure(self._breaker_threshold)
        return ok, content, err

    # -- test helpers --------------------------------------------------------

    def _breaker_state_for(self, name: str) -> _BreakerState:
        """Test-only: expose the breaker state without dragging in private attrs."""
        return self._breakers.setdefault(name, _BreakerState())
