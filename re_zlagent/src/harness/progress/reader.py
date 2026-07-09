"""Read task progress from TaskStore projections and events."""

from __future__ import annotations

from harness.storage import TaskStore
from harness.tasking import (
    Checkpoint,
    StepStatus,
    TaskEvent,
    TaskEventType,
    TaskRun,
    TaskRunStatus,
)

from .types import ProgressStatus, TaskProgressSnapshot


class TaskProgressReader:
    """Build polling-friendly progress snapshots without mutating storage."""

    def __init__(self, store: TaskStore) -> None:
        self._store = store

    def snapshot(self, run_id: str) -> TaskProgressSnapshot:
        run = self._store.get_run(run_id)
        if run is None:
            return TaskProgressSnapshot(
                run_id=run_id,
                status=ProgressStatus.MISSING,
                event_seq=0,
                event_count=0,
                checkpoint_count=0,
            )

        events = self._store.list_events(run_id)
        checkpoints = self._store.list_checkpoints(run_id)
        latest_event = events[-1] if events else None
        latest_checkpoint = checkpoints[-1] if checkpoints else None
        step_statuses = self._step_statuses(events)
        completed_steps = self._completed_steps(events)
        failed_step = self._failed_step(latest_checkpoint)
        accepted = self._accepted(events)
        blocked = self._latest_criteria(events, "blocked_criteria")
        failed = self._latest_criteria(events, "failed_criteria")

        return TaskProgressSnapshot(
            run_id=run_id,
            status=self._progress_status(run),
            event_seq=run.event_seq,
            event_count=len(events),
            checkpoint_count=len(checkpoints),
            latest_event_type=latest_event.type if latest_event else None,
            latest_checkpoint_id=latest_checkpoint.id if latest_checkpoint else None,
            latest_checkpoint_status=latest_checkpoint.status if latest_checkpoint else None,
            completed_steps=completed_steps,
            step_statuses=step_statuses,
            step_checkpoint_ids=self._step_checkpoint_ids(events),
            failed_step=failed_step,
            accepted=accepted,
            blocked_criteria=blocked,
            failed_criteria=failed,
            run_status=run.status,
            metadata={
                "current_checkpoint_id": run.current_checkpoint_id,
                "model_name": run.model_name,
                "prompt_version": run.prompt_version,
            },
        )

    def _progress_status(self, run: TaskRun) -> ProgressStatus:
        if run.status is TaskRunStatus.CREATED:
            return ProgressStatus.CREATED
        if run.status is TaskRunStatus.RUNNING:
            return ProgressStatus.RUNNING
        if run.status is TaskRunStatus.PAUSED:
            return ProgressStatus.PAUSED
        if run.status is TaskRunStatus.WAITING_USER:
            return ProgressStatus.WAITING_USER
        if run.status is TaskRunStatus.RECOVERING:
            return ProgressStatus.RECOVERING
        if run.status is TaskRunStatus.ACCEPTANCE_FAILED:
            return ProgressStatus.ACCEPTANCE_FAILED
        if run.status is TaskRunStatus.COMPLETED:
            return ProgressStatus.COMPLETED
        if run.status is TaskRunStatus.CANCELLED:
            return ProgressStatus.CANCELLED
        return ProgressStatus.FAILED

    def _completed_steps(self, events: tuple[TaskEvent, ...]) -> tuple[str, ...]:
        completed: list[str] = []
        seen: set[str] = set()
        for event in events:
            if event.type is not TaskEventType.PLAN_STEP_VERIFIED:
                continue
            step_id = event.payload.get("step_id")
            if (
                event.payload.get("status") == StepStatus.PASSED.value
                and isinstance(step_id, str)
                and step_id not in seen
            ):
                completed.append(step_id)
                seen.add(step_id)
        return tuple(completed)

    def _step_statuses(self, events: tuple[TaskEvent, ...]) -> dict[str, StepStatus]:
        statuses: dict[str, StepStatus] = {}
        for event in events:
            if event.type is TaskEventType.PLAN_STEP_STARTED:
                step_id = event.payload.get("step_id")
                if isinstance(step_id, str):
                    statuses[step_id] = StepStatus.RUNNING
                continue
            if event.type is TaskEventType.PLAN_STEP_VERIFIED:
                step_id = event.payload.get("step_id")
                status = event.payload.get("status")
                if isinstance(step_id, str) and isinstance(status, str):
                    statuses[step_id] = StepStatus(status)
        return statuses

    def _step_checkpoint_ids(self, events: tuple[TaskEvent, ...]) -> dict[str, str]:
        checkpoint_ids: dict[str, str] = {}
        for event in events:
            if event.type is not TaskEventType.PLAN_STEP_VERIFIED:
                continue
            step_id = event.payload.get("step_id")
            checkpoint_id = event.payload.get("checkpoint_id")
            if isinstance(step_id, str) and isinstance(checkpoint_id, str):
                checkpoint_ids[step_id] = checkpoint_id
        return checkpoint_ids

    def _failed_step(self, checkpoint: Checkpoint | None) -> str | None:
        if checkpoint is not None and checkpoint.failure is not None:
            return checkpoint.failure.failed_step
        return None

    def _accepted(self, events: tuple[TaskEvent, ...]) -> bool | None:
        for event in reversed(events):
            if event.type is TaskEventType.ACCEPTANCE_EVALUATED:
                accepted = event.payload.get("accepted")
                return bool(accepted) if isinstance(accepted, bool) else None
        return None

    def _latest_criteria(
        self,
        events: tuple[TaskEvent, ...],
        key: str,
    ) -> tuple[str, ...]:
        for event in reversed(events):
            if event.type is TaskEventType.ACCEPTANCE_EVALUATED:
                values = event.payload.get(key) or ()
                return tuple(str(item) for item in values)
        return ()
