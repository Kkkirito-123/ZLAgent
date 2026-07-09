"""Checkpoint and failure-envelope primitives."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from harness.tools.metadata import Evidence, SideEffect

from .contract import TaskRunStatus


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class CheckpointStatus(str, Enum):
    """Checkpoint state at the moment the snapshot was taken."""

    RUNNING = "running"
    PAUSED = "paused"
    WAITING_USER = "waiting_user"
    FAILED = "failed"
    COMPLETED = "completed"
    ACCEPTANCE_FAILED = "acceptance_failed"
    CANCELLED = "cancelled"


class FailureType(str, Enum):
    """Stable failure categories for recovery and reports."""

    TOOL_ERROR = "tool_error"
    PERMISSION_DENIED = "permission_denied"
    UNSAFE_WRITE = "unsafe_write"
    ACCEPTANCE_FAILED = "acceptance_failed"
    STEP_VERIFICATION_FAILED = "step_verification_failed"
    STALE_EVIDENCE = "stale_evidence"
    UNKNOWN = "unknown"


class FailureVisibility(str, Enum):
    """Whether the failure was explicit or inferred from semantic checks."""

    EXPLICIT = "explicit"
    IMPLICIT = "implicit"


class FailureDuration(str, Enum):
    """Whether a failure is likely transient or permanent."""

    TRANSIENT = "transient"
    PERMANENT = "permanent"
    UNKNOWN = "unknown"


class RecoveryAction(str, Enum):
    """Next recovery action selected by policy."""

    RETRY = "retry"
    READ_BEFORE_WRITE = "read_before_write"
    ASK_USER = "ask_user"
    USE_ALTERNATIVE_TOOL = "use_alternative_tool"
    MANUAL_REVIEW = "manual_review"
    STOP = "stop"


@dataclass(frozen=True, slots=True)
class FailureEnvelope:
    """Structured failure payload that can be attached to checkpoints."""

    status: TaskRunStatus
    failed_step: str
    failure_type: FailureType
    root_cause: str
    recoverable: bool
    recommended_action: RecoveryAction
    checkpoint_id: str | None = None
    evidence: tuple[Evidence, ...] = field(default_factory=tuple)
    side_effects: tuple[SideEffect, ...] = field(default_factory=tuple)
    metadata: dict[str, Any] = field(default_factory=dict)
    visibility: FailureVisibility = FailureVisibility.EXPLICIT
    duration: FailureDuration = FailureDuration.UNKNOWN

    def __post_init__(self) -> None:
        if not self.failed_step.strip():
            raise ValueError("failed_step must be non-empty")
        if not self.root_cause.strip():
            raise ValueError("root_cause must be non-empty")
        object.__setattr__(self, "evidence", tuple(self.evidence))
        object.__setattr__(self, "side_effects", tuple(self.side_effects))
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def perturbation_class(self) -> str:
        return f"{self.visibility.value}_{self.duration.value}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "failed_step": self.failed_step,
            "failure_type": self.failure_type.value,
            "root_cause": self.root_cause,
            "recoverable": self.recoverable,
            "recommended_action": self.recommended_action.value,
            "checkpoint_id": self.checkpoint_id,
            "evidence": [item.to_dict() for item in self.evidence],
            "side_effects": [item.to_dict() for item in self.side_effects],
            "metadata": dict(self.metadata),
            "visibility": self.visibility.value,
            "duration": self.duration.value,
            "perturbation_class": self.perturbation_class,
        }


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """Recoverable state snapshot for a task run."""

    id: str
    run_id: str
    seq: int
    status: CheckpointStatus
    state: dict[str, Any] = field(default_factory=dict)
    resume_from_event_id: str | None = None
    failure: FailureEnvelope | None = None
    created_at: datetime = field(default_factory=_utc_now)

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("checkpoint.id must be non-empty")
        if not self.run_id.strip():
            raise ValueError("checkpoint.run_id must be non-empty")
        if self.seq <= 0:
            raise ValueError("checkpoint.seq must be > 0")
        object.__setattr__(self, "state", dict(self.state))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "run_id": self.run_id,
            "seq": self.seq,
            "status": self.status.value,
            "state": dict(self.state),
            "resume_from_event_id": self.resume_from_event_id,
            "failure": self.failure.to_dict() if self.failure else None,
            "created_at": self.created_at.isoformat(),
        }


class CheckpointStore:
    """In-memory checkpoint store with per-run monotonic sequence."""

    def __init__(self) -> None:
        self._by_id: dict[str, Checkpoint] = {}
        self._by_run: dict[str, list[Checkpoint]] = {}

    def create(
        self,
        *,
        run_id: str,
        status: CheckpointStatus,
        state: dict[str, Any] | None = None,
        resume_from_event_id: str | None = None,
        failure: FailureEnvelope | None = None,
        checkpoint_id: str | None = None,
        created_at: datetime | None = None,
    ) -> Checkpoint:
        seq = self.last_seq(run_id) + 1
        checkpoint = Checkpoint(
            id=checkpoint_id or f"chk_{uuid4().hex}",
            run_id=run_id,
            seq=seq,
            status=status,
            state=state or {},
            resume_from_event_id=resume_from_event_id,
            failure=failure,
            created_at=created_at or _utc_now(),
        )
        if checkpoint.id in self._by_id:
            raise ValueError(f"duplicate checkpoint id: {checkpoint.id}")
        self._by_id[checkpoint.id] = checkpoint
        self._by_run.setdefault(run_id, []).append(checkpoint)
        return checkpoint

    def get(self, checkpoint_id: str) -> Checkpoint | None:
        return self._by_id.get(checkpoint_id)

    def latest(self, run_id: str) -> Checkpoint | None:
        checkpoints = self._by_run.get(run_id)
        if not checkpoints:
            return None
        return checkpoints[-1]

    def list_for_run(self, run_id: str) -> tuple[Checkpoint, ...]:
        return tuple(self._by_run.get(run_id, ()))

    def last_seq(self, run_id: str) -> int:
        checkpoints = self._by_run.get(run_id)
        if not checkpoints:
            return 0
        return checkpoints[-1].seq
