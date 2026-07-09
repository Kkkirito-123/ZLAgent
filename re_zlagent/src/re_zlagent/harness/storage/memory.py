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
    TaskContract,
    TaskEvent,
    TaskEventLog,
    TaskEventType,
    TaskRun,
)
from re_zlagent.harness.tools import Evidence, SideEffect


class InMemoryTaskStore:
    """Reference implementation for the task storage boundary.

    This class is not production storage. It defines the behavior PostgreSQL
    and other durable adapters must preserve.
    """

    def __init__(self) -> None:
        self._contracts: dict[str, TaskContract] = {}
        self._runs: dict[str, TaskRun] = {}
        self._event_log = TaskEventLog()
        self._checkpoint_store = CheckpointStore()

    def save_contract(self, contract: TaskContract) -> TaskContract:
        self._contracts[contract.id] = deepcopy(contract)
        return deepcopy(contract)

    def get_contract(self, contract_id: str) -> TaskContract | None:
        contract = self._contracts.get(contract_id)
        return deepcopy(contract) if contract is not None else None

    def create_run(self, run: TaskRun) -> TaskRun:
        if run.id in self._runs:
            raise ValueError(f"duplicate run id: {run.id}")
        if run.contract_id not in self._contracts:
            raise ValueError(f"unknown contract id: {run.contract_id}")
        self._runs[run.id] = deepcopy(run)
        return deepcopy(run)

    def get_run(self, run_id: str) -> TaskRun | None:
        run = self._runs.get(run_id)
        return deepcopy(run) if run is not None else None

    def update_run(self, run: TaskRun) -> TaskRun:
        current = self._runs.get(run.id)
        if current is None:
            raise ValueError(f"unknown run id: {run.id}")
        if run.contract_id not in self._contracts:
            raise ValueError(f"unknown contract id: {run.contract_id}")
        normalized = TaskRun(
            id=run.id,
            contract_id=run.contract_id,
            status=run.status,
            current_checkpoint_id=run.current_checkpoint_id,
            event_seq=max(run.event_seq, current.event_seq),
            model_name=run.model_name,
            prompt_version=run.prompt_version,
            metadata=run.metadata,
        )
        self._runs[run.id] = deepcopy(normalized)
        return deepcopy(normalized)

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
