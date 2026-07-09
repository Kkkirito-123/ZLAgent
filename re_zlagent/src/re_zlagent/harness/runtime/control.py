"""Runtime control actions for task runs.

This module owns operator-facing lifecycle controls such as pause, resume,
cancel, and fork. It does not execute tools and does not decide acceptance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from re_zlagent.harness.storage import TaskStore
from re_zlagent.harness.tasking import (
    Checkpoint,
    CheckpointStatus,
    TaskEvent,
    TaskEventType,
    TaskRun,
    TaskRunStatus,
)


class RunControlAction(str, Enum):
    """Operator control action names."""

    PAUSE = "pause"
    RESUME = "resume"
    CANCEL = "cancel"
    FORK = "fork"


@dataclass(frozen=True, slots=True)
class RunControlResult:
    """Structured result for a run control action."""

    accepted: bool
    action: RunControlAction
    run: TaskRun
    reason: str
    event: TaskEvent | None = None
    checkpoint: Checkpoint | None = None
    forked_run: TaskRun | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", dict(self.metadata))


class RunControlService:
    """Apply operator controls through the TaskStore lifecycle boundary."""

    _terminal_statuses = {
        TaskRunStatus.ACCEPTANCE_FAILED,
        TaskRunStatus.COMPLETED,
        TaskRunStatus.FAILED,
        TaskRunStatus.CANCELLED,
    }

    _pause_allowed = {
        TaskRunStatus.CREATED,
        TaskRunStatus.RUNNING,
        TaskRunStatus.RECOVERING,
    }

    _resume_allowed = {
        TaskRunStatus.PAUSED,
        TaskRunStatus.WAITING_USER,
    }

    def __init__(self, store: TaskStore) -> None:
        self._store = store

    def pause(
        self,
        run_id: str,
        *,
        reason: str = "",
        actor: str = "user",
    ) -> RunControlResult:
        """Pause a run that is not terminal and can be operator-paused."""

        run = self._require_run(run_id)
        if run.status not in self._pause_allowed:
            return self._reject(
                run=run,
                action=RunControlAction.PAUSE,
                reason=f"run cannot be paused from status {run.status.value}",
                actor=actor,
            )

        payload = self._payload(
            action=RunControlAction.PAUSE,
            actor=actor,
            reason=reason or "paused by operator",
            previous_status=run.status,
        )
        event = self._append_event(
            run_id=run_id,
            type=TaskEventType.RUN_PAUSED,
            payload=payload,
        )
        checkpoint = self._create_checkpoint(
            run_id=run_id,
            status=CheckpointStatus.PAUSED,
            state=payload,
            resume_from_event_id=event.id,
        )
        updated = self._set_status(
            run_id,
            TaskRunStatus.PAUSED,
            checkpoint_id=checkpoint.id,
        )
        return RunControlResult(
            accepted=True,
            action=RunControlAction.PAUSE,
            run=updated,
            reason=payload["reason"],
            event=event,
            checkpoint=checkpoint,
        )

    def resume(
        self,
        run_id: str,
        *,
        feedback: str = "",
        actor: str = "user",
    ) -> RunControlResult:
        """Resume a paused or user-blocked run with optional feedback."""

        run = self._require_run(run_id)
        if run.status not in self._resume_allowed:
            return self._reject(
                run=run,
                action=RunControlAction.RESUME,
                reason=f"run cannot be resumed from status {run.status.value}",
                actor=actor,
            )

        payload = self._payload(
            action=RunControlAction.RESUME,
            actor=actor,
            reason="resumed by operator",
            previous_status=run.status,
            feedback=feedback,
        )
        event = self._append_event(
            run_id=run_id,
            type=TaskEventType.RUN_RESUMED,
            payload=payload,
        )
        checkpoint = self._create_checkpoint(
            run_id=run_id,
            status=CheckpointStatus.RUNNING,
            state=payload,
            resume_from_event_id=event.id,
        )
        updated = self._set_status(
            run_id,
            TaskRunStatus.RUNNING,
            checkpoint_id=checkpoint.id,
        )
        return RunControlResult(
            accepted=True,
            action=RunControlAction.RESUME,
            run=updated,
            reason=payload["reason"],
            event=event,
            checkpoint=checkpoint,
            metadata={"feedback": feedback},
        )

    def cancel(
        self,
        run_id: str,
        *,
        reason: str = "",
        actor: str = "user",
    ) -> RunControlResult:
        """Cancel a non-terminal run and record a terminal checkpoint."""

        run = self._require_run(run_id)
        if run.status in self._terminal_statuses:
            return self._reject(
                run=run,
                action=RunControlAction.CANCEL,
                reason=f"run cannot be cancelled from terminal status {run.status.value}",
                actor=actor,
            )

        payload = self._payload(
            action=RunControlAction.CANCEL,
            actor=actor,
            reason=reason or "cancelled by operator",
            previous_status=run.status,
        )
        event = self._append_event(
            run_id=run_id,
            type=TaskEventType.RUN_CANCELLED,
            payload=payload,
        )
        checkpoint = self._create_checkpoint(
            run_id=run_id,
            status=CheckpointStatus.CANCELLED,
            state=payload,
            resume_from_event_id=event.id,
        )
        updated = self._set_status(
            run_id,
            TaskRunStatus.CANCELLED,
            checkpoint_id=checkpoint.id,
        )
        return RunControlResult(
            accepted=True,
            action=RunControlAction.CANCEL,
            run=updated,
            reason=payload["reason"],
            event=event,
            checkpoint=checkpoint,
        )

    def fork(
        self,
        run_id: str,
        *,
        new_run_id: str,
        reason: str = "",
        actor: str = "user",
        checkpoint_id: str | None = None,
    ) -> RunControlResult:
        """Create a new run from the same contract and record source lineage."""

        self._require_text(new_run_id, "new_run_id")
        run = self._require_run(run_id)
        if self._store.get_run(new_run_id) is not None:
            return self._reject(
                run=run,
                action=RunControlAction.FORK,
                reason=f"run already exists: {new_run_id}",
                actor=actor,
            )
        source_checkpoint_id = checkpoint_id or run.current_checkpoint_id
        if checkpoint_id is not None:
            checkpoint = self._store.get_checkpoint(checkpoint_id)
            if checkpoint is None:
                return self._reject(
                    run=run,
                    action=RunControlAction.FORK,
                    reason=f"checkpoint not found: {checkpoint_id}",
                    actor=actor,
                )
            if checkpoint.run_id != run_id:
                return self._reject(
                    run=run,
                    action=RunControlAction.FORK,
                    reason="checkpoint does not belong to run",
                    actor=actor,
                )

        fork_metadata = {
            "forked_from_run_id": run.id,
            "forked_from_checkpoint_id": source_checkpoint_id,
            "fork_reason": reason or "forked by operator",
            "fork_actor": actor,
        }
        forked_run = self._store.create_run(
            TaskRun(
                id=new_run_id,
                contract_id=run.contract_id,
                model_name=run.model_name,
                prompt_version=run.prompt_version,
                metadata=fork_metadata,
            )
        )
        self._append_event(
            run_id=forked_run.id,
            type=TaskEventType.RUN_CREATED,
            payload={
                "contract_id": forked_run.contract_id,
                "forked_from_run_id": run.id,
                "forked_from_checkpoint_id": source_checkpoint_id,
                "reason": fork_metadata["fork_reason"],
                "actor": actor,
            },
        )
        event = self._append_event(
            run_id=run_id,
            type=TaskEventType.RUN_FORKED,
            payload={
                "new_run_id": new_run_id,
                "forked_from_checkpoint_id": source_checkpoint_id,
                "reason": fork_metadata["fork_reason"],
                "actor": actor,
            },
        )
        source = self._store.get_run(run_id)
        if source is None:
            raise ValueError(f"unknown run id: {run_id}")
        forked = self._store.get_run(new_run_id)
        if forked is None:
            raise ValueError(f"unknown forked run id: {new_run_id}")
        return RunControlResult(
            accepted=True,
            action=RunControlAction.FORK,
            run=source,
            reason=fork_metadata["fork_reason"],
            event=event,
            forked_run=forked,
            metadata=fork_metadata,
        )

    def _reject(
        self,
        *,
        run: TaskRun,
        action: RunControlAction,
        reason: str,
        actor: str,
    ) -> RunControlResult:
        event = self._append_event(
            run_id=run.id,
            type=TaskEventType.RUN_CONTROL_REJECTED,
            payload={
                "action": action.value,
                "reason": reason,
                "actor": actor,
                "status": run.status.value,
            },
        )
        latest = self._store.get_run(run.id)
        if latest is None:
            raise ValueError(f"unknown run id: {run.id}")
        return RunControlResult(
            accepted=False,
            action=action,
            run=latest,
            reason=reason,
            event=event,
        )

    def _append_event(
        self,
        *,
        run_id: str,
        type: TaskEventType,
        payload: dict[str, Any],
    ) -> TaskEvent:
        return self._store.append_event(
            run_id=run_id,
            type=type,
            payload=payload,
        )

    def _create_checkpoint(
        self,
        *,
        run_id: str,
        status: CheckpointStatus,
        state: dict[str, Any],
        resume_from_event_id: str,
    ) -> Checkpoint:
        checkpoint = self._store.create_checkpoint(
            run_id=run_id,
            status=status,
            state=state,
            resume_from_event_id=resume_from_event_id,
        )
        self._append_event(
            run_id=run_id,
            type=TaskEventType.CHECKPOINT_CREATED,
            payload={
                "checkpoint_id": checkpoint.id,
                "status": checkpoint.status.value,
            },
        )
        return checkpoint

    def _set_status(
        self,
        run_id: str,
        status: TaskRunStatus,
        *,
        checkpoint_id: str | None = None,
    ) -> TaskRun:
        current = self._require_run(run_id)
        self._store.update_run(
            current.with_status(status, checkpoint_id=checkpoint_id)
        )
        self._append_event(
            run_id=run_id,
            type=TaskEventType.STATUS_CHANGED,
            payload={
                "status": status.value,
                "checkpoint_id": checkpoint_id,
            },
        )
        return self._require_run(run_id)

    def _require_run(self, run_id: str) -> TaskRun:
        self._require_text(run_id, "run_id")
        run = self._store.get_run(run_id)
        if run is None:
            raise ValueError(f"unknown run id: {run_id}")
        return run

    @staticmethod
    def _payload(
        *,
        action: RunControlAction,
        actor: str,
        reason: str,
        previous_status: TaskRunStatus,
        feedback: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "action": action.value,
            "actor": actor,
            "reason": reason,
            "previous_status": previous_status.value,
        }
        if feedback is not None:
            payload["feedback"] = feedback
        return payload

    @staticmethod
    def _require_text(value: str, field_name: str) -> None:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field_name} must be a non-empty string")
