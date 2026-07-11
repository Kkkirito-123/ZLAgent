"""Read-only scheduling views for parked long-running tasks."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable

from re_zlagent.harness.storage import LongTaskStore, TaskStore
from re_zlagent.harness.tasking import (
    Checkpoint,
    InteractionStatus,
    PendingInteraction,
    RecoveryAction,
    ResumePolicy,
    RunLeaseState,
    TaskRun,
    TaskRunStatus,
)


class ParkedRunKind(str, Enum):
    """Why a run is parked or schedulable."""

    MISSING = "missing"
    PAUSED = "paused"
    WAITING_USER = "waiting_user"
    AUTO_RETRYABLE = "auto_retryable"
    NEEDS_OPERATOR = "needs_operator"
    TERMINAL = "terminal"
    RUNNING = "running"


@dataclass(frozen=True, slots=True)
class ParkedRunCandidate:
    """Scheduler-facing read model for one run."""

    run_id: str
    kind: ParkedRunKind
    run_status: TaskRunStatus | None = None
    checkpoint_id: str | None = None
    checkpoint_status: str | None = None
    can_auto_resume: bool = False
    recommended_action: RecoveryAction | None = None
    pending_interaction_ids: tuple[str, ...] = field(default_factory=tuple)
    resume_tokens: tuple[str, ...] = field(default_factory=tuple)
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.run_id.strip():
            raise ValueError("candidate.run_id must be non-empty")
        object.__setattr__(
            self,
            "pending_interaction_ids",
            tuple(self.pending_interaction_ids),
        )
        object.__setattr__(self, "resume_tokens", tuple(self.resume_tokens))
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def parked(self) -> bool:
        return self.kind in {
            ParkedRunKind.PAUSED,
            ParkedRunKind.WAITING_USER,
            ParkedRunKind.AUTO_RETRYABLE,
            ParkedRunKind.NEEDS_OPERATOR,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "kind": self.kind.value,
            "parked": self.parked,
            "run_status": self.run_status.value if self.run_status else None,
            "checkpoint_id": self.checkpoint_id,
            "checkpoint_status": self.checkpoint_status,
            "can_auto_resume": self.can_auto_resume,
            "recommended_action": (
                self.recommended_action.value if self.recommended_action else None
            ),
            "pending_interaction_ids": list(self.pending_interaction_ids),
            "resume_tokens": list(self.resume_tokens),
            "reason": self.reason,
            "metadata": dict(self.metadata),
        }


class ParkedRunScanner:
    """Classify parked runs without mutating state or executing tools."""

    _terminal_statuses = {
        TaskRunStatus.ACCEPTANCE_FAILED,
        TaskRunStatus.COMPLETED,
        TaskRunStatus.FAILED,
        TaskRunStatus.CANCELLED,
    }

    def __init__(
        self,
        store: TaskStore,
        *,
        long_task_store: LongTaskStore | None = None,
        resume_policy: ResumePolicy | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._long_task_store = long_task_store
        self._resume_policy = resume_policy or ResumePolicy()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def inspect(self, run_id: str) -> ParkedRunCandidate:
        run = self._store.get_run(run_id)
        if run is None:
            return ParkedRunCandidate(
                run_id=run_id,
                kind=ParkedRunKind.MISSING,
                reason="run not found",
            )
        checkpoint = self._store.latest_checkpoint(run_id)
        interactions = self._open_interactions(run_id)
        lease = self._store.get_run_lease(run_id)

        if run.status is TaskRunStatus.PAUSED:
            return self._candidate(
                run=run,
                checkpoint=checkpoint,
                interactions=interactions,
                kind=ParkedRunKind.PAUSED,
                reason="run is paused by operator",
                action=RecoveryAction.ASK_USER,
            )
        if run.status is TaskRunStatus.WAITING_USER:
            return self._candidate(
                run=run,
                checkpoint=checkpoint,
                interactions=interactions,
                kind=ParkedRunKind.WAITING_USER,
                reason="run is waiting for user or operator input",
                action=RecoveryAction.ASK_USER,
            )
        if run.status in self._terminal_statuses:
            return self._candidate(
                run=run,
                checkpoint=checkpoint,
                interactions=interactions,
                kind=ParkedRunKind.TERMINAL,
                reason=f"run is terminal: {run.status.value}",
            )
        if lease is not None and lease.state is RunLeaseState.DEAD_LETTER:
            return self._candidate(
                run=run,
                checkpoint=checkpoint,
                interactions=interactions,
                kind=ParkedRunKind.NEEDS_OPERATOR,
                reason=lease.release_reason or "worker retry budget exhausted",
                action=RecoveryAction.MANUAL_REVIEW,
            )
        if lease is not None and lease.active_at(self._clock()):
            return self._candidate(
                run=run,
                checkpoint=checkpoint,
                interactions=interactions,
                kind=ParkedRunKind.RUNNING,
                reason=f"run is owned by worker {lease.owner_id}",
            )
        if (
            lease is not None
            and lease.state is RunLeaseState.BACKOFF
            and lease.next_attempt_at is not None
            and not lease.claimable_at(self._clock())
        ):
            return self._candidate(
                run=run,
                checkpoint=checkpoint,
                interactions=interactions,
                kind=ParkedRunKind.AUTO_RETRYABLE,
                reason=(
                    "run retry is in backoff until "
                    f"{lease.next_attempt_at.isoformat()}"
                ),
                action=RecoveryAction.RETRY,
                can_auto_resume=False,
            )
        if run.status is TaskRunStatus.RECOVERING:
            return self._recovering_candidate(run, checkpoint, interactions)
        return self._candidate(
            run=run,
            checkpoint=checkpoint,
            interactions=interactions,
            kind=ParkedRunKind.RUNNING,
            reason=f"run is not parked: {run.status.value}",
        )

    def scan(self, run_ids: tuple[str, ...] | list[str]) -> tuple[ParkedRunCandidate, ...]:
        return tuple(self.inspect(str(run_id)) for run_id in run_ids)

    def auto_resume_candidates(
        self,
        run_ids: tuple[str, ...] | list[str],
    ) -> tuple[ParkedRunCandidate, ...]:
        return tuple(
            candidate
            for candidate in self.scan(run_ids)
            if candidate.can_auto_resume
        )

    def _recovering_candidate(
        self,
        run: TaskRun,
        checkpoint: Checkpoint | None,
        interactions: tuple[PendingInteraction, ...],
    ) -> ParkedRunCandidate:
        if checkpoint is None:
            return self._candidate(
                run=run,
                checkpoint=checkpoint,
                interactions=interactions,
                kind=ParkedRunKind.NEEDS_OPERATOR,
                reason="recovering run has no checkpoint",
                action=RecoveryAction.MANUAL_REVIEW,
            )
        decision = self._resume_policy.decide(checkpoint)
        if decision.can_resume and decision.action is RecoveryAction.RETRY:
            return self._candidate(
                run=run,
                checkpoint=checkpoint,
                interactions=interactions,
                kind=ParkedRunKind.AUTO_RETRYABLE,
                reason=decision.reason,
                action=decision.action,
                can_auto_resume=True,
            )
        return self._candidate(
            run=run,
            checkpoint=checkpoint,
            interactions=interactions,
            kind=ParkedRunKind.NEEDS_OPERATOR,
            reason=decision.reason,
            action=decision.action,
        )

    def _candidate(
        self,
        *,
        run: TaskRun,
        checkpoint: Checkpoint | None,
        interactions: tuple[PendingInteraction, ...],
        kind: ParkedRunKind,
        reason: str,
        action: RecoveryAction | None = None,
        can_auto_resume: bool = False,
    ) -> ParkedRunCandidate:
        lease = self._store.get_run_lease(run.id)
        return ParkedRunCandidate(
            run_id=run.id,
            kind=kind,
            run_status=run.status,
            checkpoint_id=checkpoint.id if checkpoint else run.current_checkpoint_id,
            checkpoint_status=checkpoint.status.value if checkpoint else None,
            can_auto_resume=can_auto_resume,
            recommended_action=action,
            pending_interaction_ids=tuple(item.id for item in interactions),
            resume_tokens=tuple(item.resume_token for item in interactions),
            reason=reason,
            metadata={
                "event_seq": run.event_seq,
                "interaction_count": len(interactions),
                "lease": lease.to_dict() if lease is not None else None,
            },
        )

    def _open_interactions(self, run_id: str) -> tuple[PendingInteraction, ...]:
        if self._long_task_store is None:
            return ()
        return self._long_task_store.list_pending_interactions(
            run_id,
            status=InteractionStatus.OPEN,
        )
