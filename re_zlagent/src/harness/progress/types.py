"""Task progress snapshot types."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from harness.tasking import CheckpointStatus, StepStatus, TaskEventType, TaskRunStatus


class ProgressStatus(str, Enum):
    """Polling-friendly progress status."""

    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    WAITING_USER = "waiting_user"
    RECOVERING = "recovering"
    ACCEPTANCE_FAILED = "acceptance_failed"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    MISSING = "missing"


@dataclass(frozen=True, slots=True)
class TaskProgressSnapshot:
    """Read-only progress view derived from stored run state."""

    run_id: str
    status: ProgressStatus
    event_seq: int
    event_count: int
    checkpoint_count: int
    latest_event_type: TaskEventType | None = None
    latest_checkpoint_id: str | None = None
    latest_checkpoint_status: CheckpointStatus | None = None
    completed_steps: tuple[str, ...] = field(default_factory=tuple)
    step_statuses: dict[str, StepStatus] = field(default_factory=dict)
    step_checkpoint_ids: dict[str, str] = field(default_factory=dict)
    failed_step: str | None = None
    accepted: bool | None = None
    blocked_criteria: tuple[str, ...] = field(default_factory=tuple)
    failed_criteria: tuple[str, ...] = field(default_factory=tuple)
    run_status: TaskRunStatus | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "completed_steps", tuple(self.completed_steps))
        object.__setattr__(self, "step_statuses", dict(self.step_statuses))
        object.__setattr__(self, "step_checkpoint_ids", dict(self.step_checkpoint_ids))
        object.__setattr__(self, "blocked_criteria", tuple(self.blocked_criteria))
        object.__setattr__(self, "failed_criteria", tuple(self.failed_criteria))
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def terminal(self) -> bool:
        return self.status in {
            ProgressStatus.ACCEPTANCE_FAILED,
            ProgressStatus.COMPLETED,
            ProgressStatus.FAILED,
            ProgressStatus.CANCELLED,
            ProgressStatus.MISSING,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status.value,
            "terminal": self.terminal,
            "event_seq": self.event_seq,
            "event_count": self.event_count,
            "checkpoint_count": self.checkpoint_count,
            "latest_event_type": (
                self.latest_event_type.value
                if self.latest_event_type is not None
                else None
            ),
            "latest_checkpoint_id": self.latest_checkpoint_id,
            "latest_checkpoint_status": (
                self.latest_checkpoint_status.value
                if self.latest_checkpoint_status is not None
                else None
            ),
            "completed_steps": list(self.completed_steps),
            "step_statuses": {
                key: value.value
                for key, value in self.step_statuses.items()
            },
            "step_checkpoint_ids": dict(self.step_checkpoint_ids),
            "failed_step": self.failed_step,
            "accepted": self.accepted,
            "blocked_criteria": list(self.blocked_criteria),
            "failed_criteria": list(self.failed_criteria),
            "run_status": self.run_status.value if self.run_status else None,
            "metadata": dict(self.metadata),
        }
