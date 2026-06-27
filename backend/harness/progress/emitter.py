"""Per-turn progress pings during long agent runs.

IM gateways (WeChat/QQ/etc.) don't support real token streaming, so
the next best thing is *phase pings*: a short status message at
turn-start and again whenever a notable tool fires. This is the
mechanism behind that.

Design:

* Each agent turn has its own :class:`_TurnContext` held in a
  :class:`contextvars.ContextVar`. The context carries the
  per-turn send sink, dedup state (seen tool names), and the
  monotonic timestamp of the last emitted ping.
* :meth:`ProgressEmitter.tool_invoked` is called from inside the
  wrapped ``ToolRegistry.execute``; the emitter consults the policy
  map and the per-turn state to decide whether to ping.
* Two suppression rules apply:
    1. **Per-turn dedup**: each tool name pings at most once per turn,
       even if the agent calls the same tool repeatedly.
    2. **Global cooldown**: a minimum gap (default 4s) between any
       two pings of the same turn — prevents flooding when the agent
       fires several different tools in rapid succession.
* Sink calls are awaited (rather than fire-and-forget) so that send
  failures are observable, but they're best-effort: any exception is
  logged and swallowed, never surfaced to the agent.
* When no sink is bound (e.g. cron jobs, the smoke harness builder),
  every emit is a no-op. This keeps the emitter safe to attach
  globally — paths that don't bind a sink simply pay no UX cost.

Wiring:

* :func:`attach_progress` monkey-patches the live
  :class:`ToolRegistry.execute` so each call invokes
  :meth:`ProgressEmitter.tool_invoked` before the real execute. The
  patch sits *outside* the existing tracer + memo wrappers, so cache
  hits still trigger a ping (the user pays attention to the tool
  name, not the cache state).
* :func:`bind_sink` / :func:`unbind_sink` are used by ``app.py`` to
  scope a sink to the current asyncio task — typically inside
  ``_run_agent_and_dispatch``.
"""
from __future__ import annotations

import asyncio
import time
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, TYPE_CHECKING

from loguru import logger

from .policy import phase_to_text, tool_to_text

if TYPE_CHECKING:  # pragma: no cover
    from ...tools import ToolRegistry


Sink = Callable[[str], Awaitable[None]]


# ---------------------------------------------------------------------------
# Per-turn context (carried via ContextVar so asyncio tasks inherit it)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _TurnContext:
    sink: Sink
    cooldown_seconds: float
    seen_tools: set[str] = field(default_factory=set)
    last_emit_at: float = 0.0          # monotonic timestamp
    has_emitted: bool = False           # whether ANY ping was sent
    enabled: bool = True                # external switch (suppress thinking ack)
    # v0.45 — hard cap on the number of pings any single turn may emit.
    # Past complaints showed 5 "想想这事儿 / 收到正在处理 / 在网上找资料 /
    # 读取网页中 / 整理记忆" lines stacking up before the real answer
    # because every channel (realtime hint / thinking ack / slow ack /
    # per-tool ping) emitted independently with only a 4s cooldown.
    # Capping at 2 keeps one short "I'm working" signal plus at most one
    # mid-flight switch (e.g. "now reading url"), and everything past
    # that is silently dropped.
    emit_budget: int = 2
    pings_emitted: int = 0


_TURN_CTX: ContextVar[Optional[_TurnContext]] = ContextVar(
    "harness_progress_turn", default=None
)


def bind_sink(
    sink: Sink,
    *,
    cooldown_seconds: float = 4.0,
) -> Token:
    """Attach a sink to the current asyncio task's context.

    Returns the :class:`Token` from :meth:`ContextVar.set` — the
    caller must pass it to :func:`unbind_sink` when the turn is done.
    """
    ctx = _TurnContext(sink=sink, cooldown_seconds=max(0.5, float(cooldown_seconds)))
    return _TURN_CTX.set(ctx)


def unbind_sink(token: Token) -> None:
    """Restore the context to its previous (usually empty) state."""
    try:
        _TURN_CTX.reset(token)
    except (ValueError, LookupError):  # pragma: no cover — never raise upstream
        pass


def current_turn_has_emitted() -> bool:
    """True if the current turn has already sent at least one ping.

    Used by ``app.py`` to suppress the delayed "thinking…" ack when a
    tool ping already fired faster.
    """
    ctx = _TURN_CTX.get()
    return bool(ctx and ctx.has_emitted)


async def try_emit_inline_via_ctx(text: str) -> bool:
    """Module-level helper for ack-style channels without an emitter ref.

    Used by :mod:`backend.agent.turn_preparer.preparer` (travel-realtime
    loading hint) and :meth:`backend.agent.loop.AgentLoop._delayed_loading_ack`
    so those legacy direct-dispatch acks now flow through the same
    per-turn budget + cooldown that tool pings + thinking acks already
    respect. Returns True iff the ping was sent.

    Fail-soft: returns False on no-sink / over-budget / sink exception.
    """
    if not text:
        return False
    ctx = _TURN_CTX.get()
    if ctx is None or not ctx.enabled:
        return False
    if ctx.pings_emitted >= max(0, ctx.emit_budget):
        return False
    gap = time.monotonic() - ctx.last_emit_at
    if ctx.has_emitted and gap < ctx.cooldown_seconds:
        return False
    try:
        ctx.last_emit_at = time.monotonic()
        ctx.has_emitted = True
        ctx.pings_emitted += 1
        await ctx.sink(text)
        return True
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — never propagate
        logger.warning("[harness.progress] inline emit sink failed: {}", exc)
        return False


# ---------------------------------------------------------------------------
# Emitter
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ProgressStats:
    """Lightweight counters exposed via ``/api/harness/metrics``."""

    tool_pings: int = 0
    phase_pings: int = 0
    suppressed_dedup: int = 0
    suppressed_cooldown: int = 0
    suppressed_no_sink: int = 0
    sink_errors: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "tool_pings": self.tool_pings,
            "phase_pings": self.phase_pings,
            "suppressed_dedup": self.suppressed_dedup,
            "suppressed_cooldown": self.suppressed_cooldown,
            "suppressed_no_sink": self.suppressed_no_sink,
            "sink_errors": self.sink_errors,
        }


class ProgressEmitter:
    """Coordinates progress pings; one instance per process.

    Construction is cheap and side-effect-free. ``attach_progress``
    binds it to the live :class:`ToolRegistry`; ``bind_sink`` /
    ``unbind_sink`` (module-level functions above) bind a per-turn
    sink. With no sink bound, all methods are no-ops.
    """

    def __init__(self, *, default_cooldown_seconds: float = 4.0) -> None:
        self._default_cooldown = max(0.5, float(default_cooldown_seconds))
        self.stats = ProgressStats()

    @property
    def default_cooldown_seconds(self) -> float:
        return self._default_cooldown

    async def _emit_raw(self, ctx: _TurnContext, text: str) -> None:
        ctx.last_emit_at = time.monotonic()
        ctx.has_emitted = True
        ctx.pings_emitted += 1
        try:
            await ctx.sink(text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — never propagate
            self.stats.sink_errors += 1
            logger.warning("[harness.progress] sink failed: {}", exc)

    def _budget_exhausted(self, ctx: _TurnContext) -> bool:
        return ctx.pings_emitted >= max(0, ctx.emit_budget)

    async def try_emit_inline(self, text: str) -> bool:
        """Send an arbitrary ack-style ping through the per-turn sink.

        Used by external channels (realtime travel hint, slow-path
        "正在处理" ack) that don't map to a tool/phase but still count
        toward the per-turn budget. Returns True iff the message was
        actually sent.
        """
        if not text:
            return False
        ctx = _TURN_CTX.get()
        if ctx is None or not ctx.enabled:
            self.stats.suppressed_no_sink += 1
            return False
        if self._budget_exhausted(ctx):
            self.stats.suppressed_cooldown += 1
            return False
        gap = time.monotonic() - ctx.last_emit_at
        if ctx.has_emitted and gap < ctx.cooldown_seconds:
            self.stats.suppressed_cooldown += 1
            return False
        await self._emit_raw(ctx, text)
        self.stats.phase_pings += 1
        return True

    async def tool_invoked(self, tool_name: str) -> None:
        """Called once per ``ToolRegistry.execute`` invocation."""
        if not tool_name:
            return
        ctx = _TURN_CTX.get()
        if ctx is None or not ctx.enabled:
            self.stats.suppressed_no_sink += 1
            return
        if tool_name in ctx.seen_tools:
            self.stats.suppressed_dedup += 1
            return
        text = tool_to_text(tool_name)
        if text is None:
            # Silent for tools the policy doesn't map; still mark as
            # seen so a noisy follow-up label-bearing tool isn't
            # debounced unfairly.
            ctx.seen_tools.add(tool_name)
            return
        # v0.45 — per-turn hard cap. Stop emitting after the budget is
        # spent, no matter how many slow tools fire next.
        if self._budget_exhausted(ctx):
            self.stats.suppressed_cooldown += 1
            ctx.seen_tools.add(tool_name)
            return
        # Cooldown: enforce a minimum gap from the previous ping
        gap = time.monotonic() - ctx.last_emit_at
        if ctx.has_emitted and gap < ctx.cooldown_seconds:
            self.stats.suppressed_cooldown += 1
            ctx.seen_tools.add(tool_name)  # still count it as seen
            return
        ctx.seen_tools.add(tool_name)
        await self._emit_raw(ctx, text)
        self.stats.tool_pings += 1

    async def phase_changed(self, phase: str) -> None:
        """Called by app.py to surface non-tool moments (e.g. ``starting``)."""
        ctx = _TURN_CTX.get()
        if ctx is None or not ctx.enabled:
            self.stats.suppressed_no_sink += 1
            return
        text = phase_to_text(phase)
        if text is None:
            return
        # v0.45 — phase pings also bound to the per-turn budget.
        if self._budget_exhausted(ctx):
            self.stats.suppressed_cooldown += 1
            return
        # Phase pings respect cooldown too, but ignore dedup.
        gap = time.monotonic() - ctx.last_emit_at
        if ctx.has_emitted and gap < ctx.cooldown_seconds:
            self.stats.suppressed_cooldown += 1
            return
        await self._emit_raw(ctx, text)
        self.stats.phase_pings += 1

    def disable_for_current_turn(self) -> None:
        """Switch off pings for the current turn (e.g. when ack-first ran)."""
        ctx = _TURN_CTX.get()
        if ctx is not None:
            ctx.enabled = False


# ---------------------------------------------------------------------------
# Boot-time wiring
# ---------------------------------------------------------------------------


_PROGRESS_PATCHED = "_harness_progress_attached"
_PROGRESS_ORIGINAL = "_harness_progress_original_execute"


def attach_progress(
    *,
    emitter: ProgressEmitter,
    registry: "ToolRegistry",
) -> None:
    """Wrap ``registry.execute`` so every call lights up the progress emitter.

    Idempotent: a second call logs a warning and is a no-op. Detach is
    available via :func:`detach_progress`. This wrapper sits *outside*
    the tracer + memo wrappers when called in the canonical app boot
    order:

        attach_tool_memo  (innermost, sees real execute as original)
        attach_tracer     (wraps memo)
        attach_progress   (outermost — what we install here)

    Each call to ``registry.execute(name, args)`` therefore visits
    progress → tracer → memo → real-execute in that order.
    """
    if getattr(registry, _PROGRESS_PATCHED, False):
        logger.warning("[harness.progress] registry already wrapped; ignoring re-attach")
        return

    original_execute = registry.execute

    async def progress_execute(
        name: str,
        arguments: Optional[dict[str, Any]] = None,
        *,
        allow_confirm: bool = False,
    ):
        try:
            await emitter.tool_invoked(name)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — never let progress sink the call
            logger.warning("[harness.progress] tool_invoked crashed: {}", exc)
        return await original_execute(name, arguments, allow_confirm=allow_confirm)

    setattr(registry, _PROGRESS_ORIGINAL, original_execute)
    setattr(registry, _PROGRESS_PATCHED, True)
    registry.execute = progress_execute  # type: ignore[method-assign]
    logger.info("[harness.progress] attached to ToolRegistry.execute")


def detach_progress(registry: "ToolRegistry") -> bool:
    """Restore the original ``execute``; idempotent."""
    if not getattr(registry, _PROGRESS_PATCHED, False):
        return False
    original = getattr(registry, _PROGRESS_ORIGINAL, None)
    if original is None:
        return False
    registry.execute = original  # type: ignore[method-assign]
    try:
        delattr(registry, _PROGRESS_ORIGINAL)
    except AttributeError:
        pass
    setattr(registry, _PROGRESS_PATCHED, False)
    return True


__all__ = [
    "ProgressEmitter",
    "ProgressStats",
    "Sink",
    "attach_progress",
    "bind_sink",
    "current_turn_has_emitted",
    "detach_progress",
    "try_emit_inline_via_ctx",
    "unbind_sink",
]
