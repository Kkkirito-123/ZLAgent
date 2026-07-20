"""Phase-ping progress signals during long agent turns.

See :mod:`backend.harness.progress.emitter` for the design notes.

Quick wire-up reference (used by :mod:`backend.app`):

    from backend.harness.progress import (
        ProgressEmitter, bind_sink, unbind_sink,
        current_turn_has_emitted,
    )

    emitter = ProgressEmitter()
    # HarnessExecution receives the emitter during bootstrap.
    ...
    # per-turn:
    token = bind_sink(my_async_send_fn)
    try:
        await agent.run_turn(message)
    finally:
        unbind_sink(token)
"""
from .emitter import (
    ProgressEmitter,
    ProgressStats,
    Sink,
    bind_sink,
    current_turn_has_emitted,
    try_emit_inline_via_ctx,
    unbind_sink,
)

__all__ = [
    "ProgressEmitter",
    "ProgressStats",
    "Sink",
    "bind_sink",
    "current_turn_has_emitted",
    "try_emit_inline_via_ctx",
    "unbind_sink",
]
