"""Per-turn tracing for the harness observability surface.

Records compact events as they happen so the operator can answer
"why was this turn slow?" without grepping loguru output. Pure
in-memory ring buffer; no SQLite, no extra worker thread.

Two granularities are recorded:

* **Turn events**: a single row per ``AgentLoop.run_turn`` call with
  total wall time and the platform / session id.
* **Tool events**: one row per ``HarnessExecution.execute`` invocation
  with elapsed time, ``ok`` flag, and whether the call hit the memo.

Callers record events explicitly. The tracer never patches runtime methods.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Optional


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
    """One ``HarnessExecution.execute`` invocation."""

    started_at: float
    name: str
    elapsed_ms: int
    ok: bool
    error: Optional[str]
    args_preview: str
    cached: bool = False
    status: str = ""
    source: str = ""
    evidence_count: int = 0
    side_effect_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "name": self.name,
            "elapsed_ms": self.elapsed_ms,
            "ok": self.ok,
            "error": self.error,
            "args_preview": self.args_preview,
            "cached": self.cached,
            "status": self.status,
            "source": self.source,
            "evidence_count": self.evidence_count,
            "side_effect_count": self.side_effect_count,
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


__all__ = [
    "TraceRecorder",
    "TracerStats",
    "ToolRecord",
    "TurnRecord",
]
