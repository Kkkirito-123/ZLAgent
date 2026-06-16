"""Per-turn tracing for the harness observability surface.

Records compact events as they happen so the operator can answer
"why was this turn slow?" without grepping loguru output. Pure
in-memory ring buffer; no SQLite, no extra worker thread.

Two granularities are recorded:

* **Turn events**: a single row per ``AgentLoop.run_turn`` call with
  total wall time and the platform / session id.
* **Tool events**: one row per ``ToolRegistry.execute`` invocation
  with elapsed time, ``ok`` flag, and whether the call hit the memo.

The tracer attaches via :func:`attach_tracer` (boot-time
monkey-patch) so :mod:`backend.agent.loop` and
:mod:`backend.tools.registry` stay untouched.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional, TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:  # pragma: no cover
    from ...tools import ToolRegistry, ToolResult
    from ...agent.loop import AgentLoop


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class TurnRecord:
    """One ``run_turn`` invocation."""

    started_at: float
    elapsed_ms: int
    platform: str
    user_id: str
    text_preview: str
    ok: bool
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "elapsed_ms": self.elapsed_ms,
            "platform": self.platform,
            "user_id": self.user_id,
            "text_preview": self.text_preview,
            "ok": self.ok,
            "error": self.error,
        }


@dataclass(slots=True)
class ToolRecord:
    """One ``ToolRegistry.execute`` invocation."""

    started_at: float
    name: str
    elapsed_ms: int
    ok: bool
    error: Optional[str]
    args_preview: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "name": self.name,
            "elapsed_ms": self.elapsed_ms,
            "ok": self.ok,
            "error": self.error,
            "args_preview": self.args_preview,
        }


# ---------------------------------------------------------------------------
# Recorder
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class TracerStats:
    turns_recorded: int = 0
    tools_recorded: int = 0


class TraceRecorder:
    """Bounded ring buffers for turn and tool events."""

    def __init__(
        self,
        *,
        turn_capacity: int = 50,
        tool_capacity: int = 200,
    ) -> None:
        self._turns: deque[TurnRecord] = deque(maxlen=max(1, turn_capacity))
        self._tools: deque[ToolRecord] = deque(maxlen=max(1, tool_capacity))
        self.stats = TracerStats()
        self.started_monotonic = time.monotonic()

    # -- write side --------------------------------------------------------

    def record_turn(self, record: TurnRecord) -> None:
        self._turns.append(record)
        self.stats.turns_recorded += 1

    def record_tool(self, record: ToolRecord) -> None:
        self._tools.append(record)
        self.stats.tools_recorded += 1

    # -- read side --------------------------------------------------------

    def recent_turns(self, n: int = 20) -> list[dict[str, Any]]:
        n = max(1, min(int(n or 1), len(self._turns) or 1))
        return [r.to_dict() for r in list(self._turns)[-n:]]

    def recent_tools(self, n: int = 50) -> list[dict[str, Any]]:
        n = max(1, min(int(n or 1), len(self._tools) or 1))
        return [r.to_dict() for r in list(self._tools)[-n:]]

    def uptime_seconds(self) -> int:
        return int(time.monotonic() - self.started_monotonic)


# ---------------------------------------------------------------------------
# Attachment helpers (boot-time monkey-patches)
# ---------------------------------------------------------------------------


_TURN_PATCHED = "_harness_tracer_turn_patched"
_TOOL_PATCHED = "_harness_tracer_tool_patched"
_TURN_ORIG = "_harness_tracer_turn_original"
_TOOL_ORIG = "_harness_tracer_tool_original"


def attach_tracer(
    *,
    tracer: TraceRecorder,
    agent: Optional["AgentLoop"] = None,
    registry: Optional["ToolRegistry"] = None,
) -> None:
    """Wrap ``agent.run_turn`` and ``registry.execute`` with timing hooks.

    Either argument can be ``None`` (e.g. smoke environments). When
    the registry already carries the tool_memo wrapper, the tracer
    sits on top — both stay attached and behave correctly.
    """
    if agent is not None and not getattr(agent, _TURN_PATCHED, False):
        original_run_turn = agent.run_turn

        async def traced_run_turn(message, *args, **kwargs):  # type: ignore[override]
            started = time.perf_counter()
            wall_start = time.time()
            ok = True
            err: Optional[str] = None
            try:
                return await original_run_turn(message, *args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                ok = False
                err = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                preview = ""
                try:
                    raw_text = getattr(message, "text", "") or ""
                    preview = raw_text[:80]
                except Exception:  # noqa: BLE001
                    preview = ""
                tracer.record_turn(TurnRecord(
                    started_at=wall_start,
                    elapsed_ms=elapsed_ms,
                    platform=getattr(message, "platform", "") or "",
                    user_id=getattr(message, "user_id", "") or "",
                    text_preview=preview,
                    ok=ok,
                    error=err,
                ))

        setattr(agent, _TURN_ORIG, original_run_turn)
        setattr(agent, _TURN_PATCHED, True)
        agent.run_turn = traced_run_turn  # type: ignore[method-assign]
        logger.info("[harness.tracer] attached to AgentLoop.run_turn")

    if registry is not None and not getattr(registry, _TOOL_PATCHED, False):
        original_execute = registry.execute

        async def traced_execute(name, arguments=None, *, allow_confirm=False):
            started = time.perf_counter()
            wall_start = time.time()
            try:
                result = await original_execute(name, arguments, allow_confirm=allow_confirm)
            except Exception as exc:  # noqa: BLE001
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                tracer.record_tool(ToolRecord(
                    started_at=wall_start,
                    name=name,
                    elapsed_ms=elapsed_ms,
                    ok=False,
                    error=f"{type(exc).__name__}: {exc}",
                    args_preview=repr(arguments)[:80] if arguments else "",
                ))
                raise
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            tracer.record_tool(ToolRecord(
                started_at=wall_start,
                name=name,
                elapsed_ms=elapsed_ms,
                ok=bool(getattr(result, "ok", False)),
                error=getattr(result, "error", None),
                args_preview=repr(arguments)[:80] if arguments else "",
            ))
            return result

        setattr(registry, _TOOL_ORIG, original_execute)
        setattr(registry, _TOOL_PATCHED, True)
        registry.execute = traced_execute  # type: ignore[method-assign]
        logger.info("[harness.tracer] attached to ToolRegistry.execute")


def detach_tracer(
    *,
    agent: Optional["AgentLoop"] = None,
    registry: Optional["ToolRegistry"] = None,
) -> None:
    """Restore the originals; idempotent."""
    if agent is not None and getattr(agent, _TURN_PATCHED, False):
        original = getattr(agent, _TURN_ORIG, None)
        if original is not None:
            agent.run_turn = original  # type: ignore[method-assign]
        try:
            delattr(agent, _TURN_ORIG)
        except AttributeError:
            pass
        setattr(agent, _TURN_PATCHED, False)
    if registry is not None and getattr(registry, _TOOL_PATCHED, False):
        original = getattr(registry, _TOOL_ORIG, None)
        if original is not None:
            registry.execute = original  # type: ignore[method-assign]
        try:
            delattr(registry, _TOOL_ORIG)
        except AttributeError:
            pass
        setattr(registry, _TOOL_PATCHED, False)


__all__ = [
    "TraceRecorder",
    "TracerStats",
    "ToolRecord",
    "TurnRecord",
    "attach_tracer",
    "detach_tracer",
]
