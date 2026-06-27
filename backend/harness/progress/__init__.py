"""Phase-ping progress signals during long agent turns.

See :mod:`backend.harness.progress.emitter` for the design notes.

Quick wire-up reference (used by :mod:`backend.app`):

    from backend.harness.progress import (
        ProgressEmitter, attach_progress, bind_sink, unbind_sink,
        current_turn_has_emitted,
    )

    emitter = ProgressEmitter()
    attach_progress(emitter=emitter, registry=tool_registry)
    app.state.harness.progress = emitter
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
    attach_progress,
    bind_sink,
    current_turn_has_emitted,
    detach_progress,
    unbind_sink,
)

__all__ = [
    "ProgressEmitter",
    "ProgressStats",
    "Sink",
    "attach_progress",
    "bind_sink",
    "current_turn_has_emitted",
    "detach_progress",
    "unbind_sink",
]
