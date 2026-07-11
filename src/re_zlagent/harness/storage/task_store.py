"""Task persistence protocol for storage adapters."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from re_zlagent.harness.tasking import (
    Checkpoint,
    CheckpointStatus,
    FailureEnvelope,
    ProgramPlan,
    RunLease,
    RunLeaseState,
    TaskContract,
    TaskEvent,
    TaskEventType,
    TaskRun,
    TaskRunStatus,
)
from re_zlagent.harness.tools import Evidence, SideEffect


class TaskStore(Protocol):
    """Persistence boundary for task contracts, runs, events, and checkpoints.

    Database adapters must preserve these semantics:

    - contracts are stable completion definitions
    - plans are immutable executable definitions
    - runs are current-state projections with immutable contract/plan bindings
    - events are append-only history
    - checkpoints are recoverable state snapshots
    """

    def save_contract(self, contract: TaskContract) -> TaskContract:
        """Persist an immutable task contract.

        Replaying the exact same contract is idempotent. Reusing an existing id
        with different content must be rejected; a new revision needs a new id.
        """

    def get_contract(self, contract_id: str) -> TaskContract | None:
        """Return a task contract by id."""

    def save_plan(self, plan: ProgramPlan) -> ProgramPlan:
        """Persist an immutable executable plan.

        Replaying the exact same plan is idempotent. Reusing an existing id with
        different content must be rejected.
        """

    def get_plan(self, plan_id: str) -> ProgramPlan | None:
        """Return an executable plan by id."""

    def create_run(self, run: TaskRun) -> TaskRun:
        """Create a run projection for an existing contract."""

    def get_run(self, run_id: str) -> TaskRun | None:
        """Return a run projection by id."""

    def list_runs(
        self,
        *,
        statuses: tuple[TaskRunStatus, ...] | None = None,
        limit: int = 100,
    ) -> tuple[TaskRun, ...]:
        """List run projections for worker scanning."""

    def update_run(self, run: TaskRun) -> TaskRun:
        """Replace the current run projection without changing event history."""

    def compare_and_set_run_status(
        self,
        run_id: str,
        *,
        expected_statuses: tuple[TaskRunStatus, ...],
        status: TaskRunStatus,
        checkpoint_id: str | None = None,
    ) -> TaskRun | None:
        """Atomically update status only when the current status is expected."""

    def get_run_lease(self, run_id: str) -> RunLease | None:
        """Return the durable worker lease/retry projection for a run."""

    def claim_run(
        self,
        run_id: str,
        *,
        owner_id: str,
        lease_seconds: int,
        retry_budget: int,
        now: datetime | None = None,
    ) -> RunLease | None:
        """Atomically claim an eligible run or return None."""

    def heartbeat_run_lease(
        self,
        run_id: str,
        *,
        owner_id: str,
        lease_token: str,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> RunLease:
        """Extend an active lease owned by the caller."""

    def release_run_lease(
        self,
        run_id: str,
        *,
        owner_id: str,
        lease_token: str,
        state: RunLeaseState,
        reason: str,
        next_attempt_at: datetime | None = None,
        last_error: str | None = None,
    ) -> RunLease:
        """Release an active lease into a durable scheduling state."""

    def append_event(
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
        """Append one task event and return the persisted event."""

    def list_events(self, run_id: str) -> tuple[TaskEvent, ...]:
        """Return all events for a run in sequence order."""

    def create_checkpoint(
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
        """Persist a checkpoint and update the run projection anchor."""

    def get_checkpoint(self, checkpoint_id: str) -> Checkpoint | None:
        """Return a checkpoint by id."""

    def latest_checkpoint(self, run_id: str) -> Checkpoint | None:
        """Return the latest checkpoint for a run."""

    def list_checkpoints(self, run_id: str) -> tuple[Checkpoint, ...]:
        """Return all checkpoints for a run in sequence order."""


def ensure_immutable_contract_replay(
    existing: TaskContract,
    candidate: TaskContract,
) -> TaskContract:
    """Return an idempotent replay or reject an identity-changing overwrite."""

    if existing != candidate:
        raise ValueError(
            f"contract id already exists with different content: {candidate.id}"
        )
    return existing


def ensure_immutable_plan_replay(
    existing: ProgramPlan,
    candidate: ProgramPlan,
) -> ProgramPlan:
    """Return an idempotent plan replay or reject an identity overwrite."""

    if existing != candidate:
        raise ValueError(f"plan id already exists with different content: {candidate.id}")
    return existing


def ensure_plan_matches_run_contract(
    run: TaskRun,
    plan: ProgramPlan,
) -> None:
    """Reject cross-contract plan bindings before a run is persisted."""

    if run.plan_id != plan.id:
        raise ValueError("run plan id does not match loaded plan")
    if run.contract_id != plan.contract_id:
        raise ValueError("run contract does not match plan contract")


def ensure_immutable_run_binding(existing: TaskRun, candidate: TaskRun) -> None:
    """Keep a run attached to its original contract and plan revision."""

    if existing.contract_id != candidate.contract_id:
        raise ValueError("run contract binding is immutable")
    if existing.plan_id != candidate.plan_id:
        raise ValueError("run plan binding is immutable")
