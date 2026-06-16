"""Write-origin provenance primitive (ContextVar-based).

Tracks who initiated the current write — a foreground user request
or a background self-review fork. Used by:

* ``agent/loop.py`` to mark the review fork's writes.
* ``skills/guard.py`` to relax policy on user-initiated writes.
* ``skills/history.py`` to record the actor in the history JSONL.
* ``tools/builtins/skill_manage.py`` to tag ``created_by`` on
  skill records.

Belongs to the core layer: ContextVar primitive with no upward
dependencies. Previously lived in ``agent/skill_provenance.py``
but the agent does not own the concept — it's a shared marker
that multiple subsystems read.
"""
from __future__ import annotations

import contextvars

FOREGROUND = "foreground"
BACKGROUND_REVIEW = "background_review"

_write_origin: contextvars.ContextVar[str] = contextvars.ContextVar(
    "zlagent_skill_write_origin",
    default=FOREGROUND,
)


def set_current_write_origin(origin: str) -> contextvars.Token[str]:
    """Bind ``origin`` to the current async context.

    Pair with :func:`reset_current_write_origin` in a finally block.
    Empty input normalises back to ``FOREGROUND``.
    """
    return _write_origin.set(origin or FOREGROUND)


def reset_current_write_origin(token: contextvars.Token[str]) -> None:
    """Restore the prior binding using the token returned by ``set``."""
    _write_origin.reset(token)


def get_current_write_origin() -> str:
    """Return the active write origin."""
    return _write_origin.get()


def is_background_review() -> bool:
    """Convenience: is the current write the agent's own review fork?"""
    return get_current_write_origin() == BACKGROUND_REVIEW


__all__ = [
    "BACKGROUND_REVIEW",
    "FOREGROUND",
    "get_current_write_origin",
    "is_background_review",
    "reset_current_write_origin",
    "set_current_write_origin",
]
