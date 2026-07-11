"""Long-task progress snapshots."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from re_zlagent.harness.runtime import ParkedRunCandidate, ParkedRunScanner
from re_zlagent.harness.storage import LongTaskStore, TaskStore
from re_zlagent.harness.tasking import (
    DagExecutionAssessment,
    DagExecutionPolicy,
    LongTaskProjection,
    LongTaskProjector,
    ProgramPlan,
)

from .reader import TaskProgressReader
from .types import TaskProgressSnapshot


@dataclass(frozen=True, slots=True)
class LongTaskProgressSnapshot:
    """Read-only progress view for a long-running DAG task."""

    run_id: str
    task: TaskProgressSnapshot
    projection: LongTaskProjection
    parked: ParkedRunCandidate
    dag_execution: DagExecutionAssessment
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.run_id.strip():
            raise ValueError("long_task_progress.run_id must be non-empty")
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task": self.task.to_dict(),
            "projection": self.projection.to_dict(),
            "parked": self.parked.to_dict(),
            "dag_execution": self.dag_execution.to_dict(),
            "metadata": dict(self.metadata),
        }


class LongTaskProgressReader:
    """Build long-task progress without mutating storage."""

    def __init__(
        self,
        store: TaskStore,
        *,
        long_task_store: LongTaskStore | None = None,
        projector: LongTaskProjector | None = None,
        dag_execution_policy: DagExecutionPolicy | None = None,
    ) -> None:
        self._store = store
        self._long_task_store = long_task_store
        self._task_reader = TaskProgressReader(store)
        self._projector = projector or LongTaskProjector()
        self._dag_execution_policy = dag_execution_policy or DagExecutionPolicy()
        self._parked_scanner = ParkedRunScanner(
            store,
            long_task_store=long_task_store,
        )

    def snapshot(
        self,
        run_id: str,
        *,
        program_plan: ProgramPlan,
    ) -> LongTaskProgressSnapshot:
        task = self._task_reader.snapshot(run_id)
        events = self._store.list_events(run_id) if self._store.get_run(run_id) else ()
        checkpoints = (
            self._store.list_checkpoints(run_id)
            if self._store.get_run(run_id)
            else ()
        )
        interactions = (
            self._long_task_store.list_pending_interactions(run_id)
            if self._long_task_store is not None and self._store.get_run(run_id)
            else ()
        )
        projection = self._projector.project(
            run_id=run_id,
            program_plan=program_plan,
            events=events,
            checkpoints=checkpoints,
            pending_interactions=interactions,
        )
        dag_execution = self._dag_execution_policy.assess(
            program_plan=program_plan,
            projection=projection,
        )
        parked = self._parked_scanner.inspect(run_id)
        return LongTaskProgressSnapshot(
            run_id=run_id,
            task=task,
            projection=projection,
            parked=parked,
            dag_execution=dag_execution,
            metadata={
                "pending_interaction_count": len(projection.pending_interaction_ids),
                "frontier_count": len(projection.frontier_step_ids),
                "can_parallelize": dag_execution.can_parallelize,
            },
        )
