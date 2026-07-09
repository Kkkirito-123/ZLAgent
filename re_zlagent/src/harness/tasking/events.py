"""Append-only task event primitives."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from harness.tools.metadata import Evidence, SideEffect


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TaskEventType(str, Enum):
    """Event categories emitted by the task lifecycle."""

    RUN_CREATED = "run_created"
    STATUS_CHANGED = "status_changed"
    PLAN_STEP_STARTED = "plan_step_started"
    PLAN_STEP_VERIFIED = "plan_step_verified"
    TOOL_RESULT_RECORDED = "tool_result_recorded"
    ACCEPTANCE_EVALUATED = "acceptance_evaluated"
    CHECKPOINT_CREATED = "checkpoint_created"
    USER_INPUT_REQUIRED = "user_input_required"
    USER_INPUT_RECORDED = "user_input_recorded"
    RUN_PAUSED = "run_paused"
    RUN_RESUMED = "run_resumed"
    RUN_FORKED = "run_forked"
    RUN_CONTROL_REJECTED = "run_control_rejected"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"
    RUN_CANCELLED = "run_cancelled"


@dataclass(frozen=True, slots=True)
class TaskEvent:
    """Immutable task event for trace, replay, and support bundles."""

    id: str
    run_id: str
    seq: int
    type: TaskEventType
    payload: dict[str, Any] = field(default_factory=dict)
    evidence: tuple[Evidence, ...] = field(default_factory=tuple)
    side_effects: tuple[SideEffect, ...] = field(default_factory=tuple)
    created_at: datetime = field(default_factory=_utc_now)
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("event.id must be non-empty")
        if not self.run_id.strip():
            raise ValueError("event.run_id must be non-empty")
        if self.seq <= 0:
            raise ValueError("event.seq must be > 0")
        object.__setattr__(self, "payload", dict(self.payload))
        object.__setattr__(self, "evidence", tuple(self.evidence))
        object.__setattr__(self, "side_effects", tuple(self.side_effects))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "run_id": self.run_id,
            "seq": self.seq,
            "type": self.type.value,
            "payload": dict(self.payload),
            "evidence": [item.to_dict() for item in self.evidence],
            "side_effects": [item.to_dict() for item in self.side_effects],
            "created_at": self.created_at.isoformat(),
            "idempotency_key": self.idempotency_key,
        }


class TaskEventLog:
    """Small in-memory append-only event log.

    Storage adapters should preserve these semantics: append only, per-run
    monotonic sequence, and idempotent retries by key.
    """

    def __init__(self) -> None:
        self._events: list[TaskEvent] = []
        self._by_run: dict[str, list[TaskEvent]] = {}
        self._idempotency: dict[tuple[str, str], TaskEvent] = {}

    def append(
        self,
        *,
        run_id: str,
        type: TaskEventType,
        payload: dict[str, Any] | None = None,
        evidence: tuple[Evidence, ...] | list[Evidence] = (),
        side_effects: tuple[SideEffect, ...] | list[SideEffect] = (),
        idempotency_key: str | None = None,
        event_id: str | None = None,
        created_at: datetime | None = None,
    ) -> TaskEvent:
        if idempotency_key:
            existing = self._idempotency.get((run_id, idempotency_key))
            if existing is not None:
                return existing

        seq = self.last_seq(run_id) + 1
        event = TaskEvent(
            id=event_id or f"evt_{uuid4().hex}",
            run_id=run_id,
            seq=seq,
            type=type,
            payload=payload or {},
            evidence=tuple(evidence),
            side_effects=tuple(side_effects),
            created_at=created_at or _utc_now(),
            idempotency_key=idempotency_key,
        )
        self._events.append(event)
        self._by_run.setdefault(run_id, []).append(event)
        if idempotency_key:
            self._idempotency[(run_id, idempotency_key)] = event
        return event

    def list_for_run(self, run_id: str) -> tuple[TaskEvent, ...]:
        return tuple(self._by_run.get(run_id, ()))

    def last_seq(self, run_id: str) -> int:
        events = self._by_run.get(run_id)
        if not events:
            return 0
        return events[-1].seq

    def all_events(self) -> tuple[TaskEvent, ...]:
        return tuple(self._events)
