"""In-memory task store used as the storage adapter behavior baseline."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any

from re_zlagent.harness.tasking import (
    Checkpoint,
    CheckpointStatus,
    CheckpointStore,
    FailureEnvelope,
    ProgramPlan,
    RunLease,
    RunLeaseState,
    TaskContract,
    TaskEvent,
    TaskEventLog,
    TaskEventType,
    TaskRun,
    TaskRunStatus,
)
from re_zlagent.harness.tools import Evidence, SideEffect

from .task_store import (
    ensure_immutable_contract_replay,
    ensure_immutable_plan_replay,
    ensure_immutable_run_binding,
    ensure_plan_matches_run_contract,
)


class InMemoryTaskStore:
    """Reference implementation for the task storage boundary.

    This class is not production storage. It defines the behavior PostgreSQL
    and other durable adapters must preserve.
    """

    def __init__(self) -> None:
        self._contracts: dict[str, TaskContract] = {}
        self._plans: dict[str, ProgramPlan] = {}
        self._runs: dict[str, TaskRun] = {}
        self._run_leases: dict[str, RunLease] = {}
        self._event_log = TaskEventLog()
        self._checkpoint_store = CheckpointStore()

    def save_contract(self, contract: TaskContract) -> TaskContract:
        existing = self._contracts.get(contract.id)
        if existing is not None:
            return deepcopy(ensure_immutable_contract_replay(existing, contract))
        self._contracts[contract.id] = deepcopy(contract)
        return deepcopy(contract)

    def get_contract(self, contract_id: str) -> TaskContract | None:
        contract = self._contracts.get(contract_id)
        return deepcopy(contract) if contract is not None else None

    def save_plan(self, plan: ProgramPlan) -> ProgramPlan:
        if plan.contract_id not in self._contracts:
            raise ValueError(f"unknown contract id: {plan.contract_id}")
        existing = self._plans.get(plan.id)
        if existing is not None:
            return deepcopy(ensure_immutable_plan_replay(existing, plan))
        self._plans[plan.id] = deepcopy(plan)
        return deepcopy(plan)

    def get_plan(self, plan_id: str) -> ProgramPlan | None:
        plan = self._plans.get(plan_id)
        return deepcopy(plan) if plan is not None else None

    def create_run(self, run: TaskRun) -> TaskRun:
        if run.id in self._runs:
            raise ValueError(f"duplicate run id: {run.id}")
        if run.contract_id not in self._contracts:
            raise ValueError(f"unknown contract id: {run.contract_id}")
        if run.plan_id is not None:
            plan = self._plans.get(run.plan_id)
            if plan is None:
                raise ValueError(f"unknown plan id: {run.plan_id}")
            ensure_plan_matches_run_contract(run, plan)
        self._runs[run.id] = deepcopy(run)
        return deepcopy(run)

    def get_run(self, run_id: str) -> TaskRun | None:
        run = self._runs.get(run_id)
        return deepcopy(run) if run is not None else None

    def list_runs(
        self,
        *,
        statuses: tuple[TaskRunStatus, ...] | None = None,
        limit: int = 100,
    ) -> tuple[TaskRun, ...]:
        if limit <= 0:
            raise ValueError("limit must be > 0")
        allowed = set(statuses) if statuses is not None else None
        runs = (
            run
            for run in self._runs.values()
            if allowed is None or run.status in allowed
        )
        return deepcopy(tuple(runs)[:limit])

    def update_run(self, run: TaskRun) -> TaskRun:
        current = self._runs.get(run.id)
        if current is None:
            raise ValueError(f"unknown run id: {run.id}")
        ensure_immutable_run_binding(current, run)
        if run.contract_id not in self._contracts:
            raise ValueError(f"unknown contract id: {run.contract_id}")
        if run.plan_id is not None:
            plan = self._plans.get(run.plan_id)
            if plan is None:
                raise ValueError(f"unknown plan id: {run.plan_id}")
            ensure_plan_matches_run_contract(run, plan)
        normalized = TaskRun(
            id=run.id,
            contract_id=run.contract_id,
            plan_id=run.plan_id,
            status=run.status,
            current_checkpoint_id=run.current_checkpoint_id,
            event_seq=max(run.event_seq, current.event_seq),
            model_name=run.model_name,
            prompt_version=run.prompt_version,
            metadata=run.metadata,
        )
        self._runs[run.id] = deepcopy(normalized)
        return deepcopy(normalized)

    def compare_and_set_run_status(
        self,
        run_id: str,
        *,
        expected_statuses: tuple[TaskRunStatus, ...],
        status: TaskRunStatus,
        checkpoint_id: str | None = None,
    ) -> TaskRun | None:
        current = self._require_run(run_id)
        if current.status not in set(expected_statuses):
            return None
        updated = current.with_status(status, checkpoint_id=checkpoint_id)
        self._runs[run_id] = deepcopy(updated)
        return deepcopy(updated)

    def get_run_lease(self, run_id: str) -> RunLease | None:
        self._require_run(run_id)
        lease = self._run_leases.get(run_id)
        return deepcopy(lease) if lease is not None else None

    def claim_run(
        self,
        run_id: str,
        *,
        owner_id: str,
        lease_seconds: int,
        retry_budget: int,
        now: datetime | None = None,
    ) -> RunLease | None:
        run = self._require_run(run_id)
        if run.status not in {
            TaskRunStatus.CREATED,
            TaskRunStatus.RUNNING,
            TaskRunStatus.RECOVERING,
        }:
            return None
        current = self._run_leases.get(run_id) or RunLease(
            run_id=run_id,
            retry_budget=retry_budget,
        )
        if not current.claimable_at(now):
            return None
        if current.attempt_count >= current.retry_budget:
            self._run_leases[run_id] = current.dead_letter(
                "retry budget exhausted"
            )
            return None
        claimed = current.claim(
            owner_id=owner_id,
            lease_seconds=lease_seconds,
            now=now,
        )
        self._run_leases[run_id] = claimed
        return deepcopy(claimed)

    def heartbeat_run_lease(
        self,
        run_id: str,
        *,
        owner_id: str,
        lease_token: str,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> RunLease:
        run = self._require_run(run_id)
        if run.status not in {
            TaskRunStatus.CREATED,
            TaskRunStatus.RUNNING,
            TaskRunStatus.RECOVERING,
        }:
            raise ValueError(f"run is not executable: {run.status.value}")
        current = self._run_leases.get(run_id)
        if current is None:
            raise ValueError(f"run has no lease: {run_id}")
        updated = current.heartbeat(
            owner_id=owner_id,
            lease_token=lease_token,
            lease_seconds=lease_seconds,
            now=now,
        )
        self._run_leases[run_id] = updated
        return deepcopy(updated)

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
        self._require_run(run_id)
        current = self._run_leases.get(run_id)
        if current is None:
            raise ValueError(f"run has no lease: {run_id}")
        updated = current.release(
            owner_id=owner_id,
            lease_token=lease_token,
            state=state,
            reason=reason,
            next_attempt_at=next_attempt_at,
            last_error=last_error,
        )
        self._run_leases[run_id] = updated
        return deepcopy(updated)

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
        run = self._require_run(run_id)
        event = self._event_log.append(
            run_id=run_id,
            type=type,
            payload=payload,
            evidence=evidence,
            side_effects=side_effects,
            idempotency_key=idempotency_key,
            event_id=event_id,
            created_at=created_at,
        )
        if event.seq > run.event_seq:
            self._runs[run_id] = run.with_status(run.status, event_seq=event.seq)
        return deepcopy(event)

    def list_events(self, run_id: str) -> tuple[TaskEvent, ...]:
        self._require_run(run_id)
        return deepcopy(self._event_log.list_for_run(run_id))

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
        run = self._require_run(run_id)
        checkpoint = self._checkpoint_store.create(
            run_id=run_id,
            status=status,
            state=state,
            resume_from_event_id=resume_from_event_id,
            failure=failure,
            checkpoint_id=checkpoint_id,
            created_at=created_at,
        )
        self._runs[run_id] = run.with_status(
            run.status,
            checkpoint_id=checkpoint.id,
            event_seq=run.event_seq,
        )
        return deepcopy(checkpoint)

    def get_checkpoint(self, checkpoint_id: str) -> Checkpoint | None:
        checkpoint = self._checkpoint_store.get(checkpoint_id)
        return deepcopy(checkpoint) if checkpoint is not None else None

    def latest_checkpoint(self, run_id: str) -> Checkpoint | None:
        self._require_run(run_id)
        checkpoint = self._checkpoint_store.latest(run_id)
        return deepcopy(checkpoint) if checkpoint is not None else None

    def list_checkpoints(self, run_id: str) -> tuple[Checkpoint, ...]:
        self._require_run(run_id)
        return deepcopy(self._checkpoint_store.list_for_run(run_id))

    def _require_run(self, run_id: str) -> TaskRun:
        run = self._runs.get(run_id)
        if run is None:
            raise ValueError(f"unknown run id: {run_id}")
        return run
