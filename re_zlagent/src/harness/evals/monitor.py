"""Realtime health snapshots for agent runs."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from harness.agent import AgentRunResult
from harness.tasking import RecoveryAction, TaskRunStatus


class RunHealthStatus(str, Enum):
    """Operational health bucket for a runtime result."""

    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    WAITING_USER = "waiting_user"
    RECOVERABLE = "recoverable"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class RunHealthSnapshot:
    """Read-only monitoring view of a run result."""

    run_id: str
    status: RunHealthStatus
    accepted: bool
    terminal: bool
    event_seq: int
    latest_checkpoint_id: str | None = None
    next_action: RecoveryAction | None = None
    failure_type: str | None = None
    blocked_criteria: tuple[str, ...] = field(default_factory=tuple)
    failed_criteria: tuple[str, ...] = field(default_factory=tuple)
    tool_failure_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "blocked_criteria", tuple(self.blocked_criteria))
        object.__setattr__(self, "failed_criteria", tuple(self.failed_criteria))
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status.value,
            "accepted": self.accepted,
            "terminal": self.terminal,
            "event_seq": self.event_seq,
            "latest_checkpoint_id": self.latest_checkpoint_id,
            "next_action": self.next_action.value if self.next_action else None,
            "failure_type": self.failure_type,
            "blocked_criteria": list(self.blocked_criteria),
            "failed_criteria": list(self.failed_criteria),
            "tool_failure_count": self.tool_failure_count,
            "metadata": dict(self.metadata),
        }


class RunHealthMonitor:
    """Build health snapshots without mutating task state."""

    def inspect(self, result: AgentRunResult) -> RunHealthSnapshot:
        runtime = result.runtime_result
        run = runtime.run
        failure = runtime.failure
        decision = runtime.acceptance_decision
        latest_checkpoint_id = (
            runtime.checkpoints[-1].id
            if runtime.checkpoints
            else run.current_checkpoint_id
        )
        tool_failure_count = sum(1 for item in runtime.tool_results if not item.ok)

        status = RunHealthStatus.RUNNING
        terminal = False
        next_action = None
        failure_type = None

        if run.status is TaskRunStatus.COMPLETED:
            status = RunHealthStatus.COMPLETED
            terminal = True
        elif run.status is TaskRunStatus.PAUSED:
            status = RunHealthStatus.PAUSED
            next_action = RecoveryAction.ASK_USER
        elif run.status is TaskRunStatus.WAITING_USER:
            status = RunHealthStatus.WAITING_USER
            next_action = failure.recommended_action if failure else RecoveryAction.ASK_USER
        elif run.status is TaskRunStatus.RECOVERING:
            status = RunHealthStatus.RECOVERABLE
            next_action = failure.recommended_action if failure else RecoveryAction.RETRY
        elif run.status in {
            TaskRunStatus.ACCEPTANCE_FAILED,
            TaskRunStatus.FAILED,
            TaskRunStatus.CANCELLED,
        }:
            status = RunHealthStatus.FAILED
            terminal = True
            next_action = failure.recommended_action if failure else RecoveryAction.STOP

        if failure is not None:
            failure_type = failure.failure_type.value

        blocked = decision.blocked_criteria if decision is not None else ()
        failed = decision.failed_criteria if decision is not None else ()
        return RunHealthSnapshot(
            run_id=run.id,
            status=status,
            accepted=result.accepted,
            terminal=terminal,
            event_seq=run.event_seq,
            latest_checkpoint_id=latest_checkpoint_id,
            next_action=next_action,
            failure_type=failure_type,
            blocked_criteria=blocked,
            failed_criteria=failed,
            tool_failure_count=tool_failure_count,
            metadata={
                "run_status": run.status.value,
                "event_count": len(runtime.events),
                "checkpoint_count": len(runtime.checkpoints),
            },
        )
