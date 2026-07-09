"""Task persistence protocol for storage adapters."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from harness.tasking import (
    Checkpoint,
    CheckpointStatus,
    FailureEnvelope,
    TaskContract,
    TaskEvent,
    TaskEventType,
    TaskRun,
)
from harness.tools import Evidence, SideEffect


class TaskStore(Protocol):
    """Persistence boundary for task contracts, runs, events, and checkpoints.

    Database adapters must preserve these semantics:

    - contracts are stable completion definitions
    - runs are current-state projections
    - events are append-only history
    - checkpoints are recoverable state snapshots
    """

    def save_contract(self, contract: TaskContract) -> TaskContract:
        """Persist a task contract."""

    def get_contract(self, contract_id: str) -> TaskContract | None:
        """Return a task contract by id."""

    def create_run(self, run: TaskRun) -> TaskRun:
        """Create a run projection for an existing contract."""

    def get_run(self, run_id: str) -> TaskRun | None:
        """Return a run projection by id."""

    def update_run(self, run: TaskRun) -> TaskRun:
        """Replace the current run projection without changing event history."""

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
