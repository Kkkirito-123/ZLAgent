"""Per-turn context exposed to tools via :class:`contextvars.ContextVar`.

Background
----------
Some tools — most notably ``cron_manage`` (v0.8) — need to know *who* the
current turn is talking to so they can default things like the cron job's
delivery target back to "the chat the user is currently messaging from".
Hermes Agent solves this by stuffing ``HERMES_SESSION_PLATFORM`` /
``HERMES_SESSION_CHAT_ID`` into a process env scope; we use Python's
:mod:`contextvars` because we already run as a single asyncio process and
this scopes cleanly per coroutine without polluting the global env.

Usage
-----
:class:`backend.agent.loop.AgentLoop` sets the context at the very top of
each :meth:`run_turn` call (via :func:`set_turn_context`) and resets it on
exit. Tools that *need* the context import :func:`current_turn_context`
and treat ``None`` as "no IM session is currently active" (e.g. an offline
smoke test driving the tool directly).
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Optional

from .state import TurnContext


_TURN_CTX: ContextVar[Optional[TurnContext]] = ContextVar("zlagent_turn_ctx", default=None)


def current_turn_context() -> Optional[TurnContext]:
    """Return the active :class:`TurnContext` or ``None`` outside of a turn."""
    return _TURN_CTX.get()


@contextmanager
def set_turn_context(ctx: TurnContext) -> Iterator[None]:
    """Bind ``ctx`` for the duration of the ``with`` block.

    Always pairs the set with a token-based reset so concurrent turns (which
    we don't have today, but may once the gateway grows real concurrency)
    can't bleed context into each other.
    """
    token = _TURN_CTX.set(ctx)
    try:
        yield
    finally:
        _TURN_CTX.reset(token)
