"""Concrete local JSON adapter over application and runtime services."""

from __future__ import annotations

from typing import Any

from re_zlagent.harness.agent import AgentOrchestrator, AgentRunRequest
from re_zlagent.harness.runtime import DurableWorker, WorkerTickResult
from re_zlagent.harness.storage import LongTaskStore, TaskStore
from re_zlagent.harness.storage.serde import (
    contract_to_dict,
    event_to_dict,
    run_to_dict,
)
from re_zlagent.harness.tasking import (
    InteractionStatus,
    TaskEventType,
    TaskRunStatus,
)

from .operator import ApprovalService, OperatorService, runtime_result_to_dict


class LocalTaskAdapter:
    """Thin JSON-compatible product adapter for one local durable workspace."""

    def __init__(
        self,
        *,
        store: TaskStore,
        long_task_store: LongTaskStore,
        operator: OperatorService,
        approvals: ApprovalService,
        worker: DurableWorker,
        orchestrator: AgentOrchestrator | None = None,
    ) -> None:
        self._store = store
        self._long_task_store = long_task_store
        self._operator = operator
        self._approvals = approvals
        self._worker = worker
        self._orchestrator = orchestrator

    async def submit(self, request: AgentRunRequest) -> dict[str, Any]:
        """Plan and persist a task without dispatching its tools."""

        if self._orchestrator is None:
            raise ValueError("submission requires a configured planner")
        result = await self._orchestrator.submit(request)
        submission = result.runtime_submission
        return {
            "ok": True,
            "command": "submit",
            "run_id": submission.run.id,
            "submission": {
                "run": run_to_dict(submission.run),
                "contract": contract_to_dict(submission.contract),
                "plan": submission.plan.to_dict(),
                "planner_metadata": dict(result.planner_metadata),
            },
        }

    def status(self, run_id: str) -> dict[str, Any]:
        """Return progress plus durable user interactions."""

        data = self._operator.status(run_id).to_dict()
        if data["ok"]:
            data["pending_interactions"] = [
                item.to_dict()
                for item in self._long_task_store.list_pending_interactions(
                    run_id,
                    status=InteractionStatus.OPEN,
                )
            ]
        return data

    async def approve(
        self,
        run_id: str,
        *,
        checkpoint_id: str | None = None,
        resume_token: str | None = None,
        feedback: str = "",
    ) -> dict[str, Any]:
        """Resolve one durable approval wait point through HarnessRuntime."""

        if resume_token is not None:
            interaction = self._long_task_store.get_pending_interaction_by_resume_token(
                resume_token
            )
            if interaction is None:
                raise ValueError("unknown pending interaction resume token")
            if interaction.run_id != run_id:
                raise ValueError("resume token belongs to a different run")
            if interaction.status is not InteractionStatus.OPEN:
                raise ValueError("pending interaction is already resolved")
            if checkpoint_id is not None and checkpoint_id != interaction.checkpoint_id:
                raise ValueError("checkpoint id does not match resume token")
            checkpoint_id = interaction.checkpoint_id
        response = await self._approvals.approve_checkpoint(
            run_id,
            checkpoint_id=checkpoint_id,
            feedback=feedback,
        )
        return response.to_dict()

    async def work_once(self, run_id: str | None = None) -> dict[str, Any]:
        """Claim and execute at most one durable run."""

        if run_id is not None and self._store.get_run(run_id) is None:
            return {
                "ok": False,
                "command": "work",
                "run_id": run_id,
                "status": "missing",
                "reason": f"unknown run id: {run_id}",
                "error": {
                    "type": "not_found",
                    "message": f"unknown run id: {run_id}",
                },
            }
        tick = await self._worker.run_once(run_id)
        return worker_tick_to_dict(tick)

    def result(self, run_id: str) -> dict[str, Any]:
        """Read persisted outputs and acceptance truth without re-execution."""

        run = self._store.get_run(run_id)
        if run is None:
            return {
                "ok": False,
                "command": "result",
                "run_id": run_id,
                "error": {
                    "type": "not_found",
                    "message": f"unknown run id: {run_id}",
                },
            }
        progress = self._operator.status(run_id).progress
        if progress is None:
            raise RuntimeError(f"progress disappeared for run: {run_id}")
        events = self._store.list_events(run_id)
        checkpoints = self._store.list_checkpoints(run_id)
        acceptance_event = next(
            (
                event
                for event in reversed(events)
                if event.type is TaskEventType.ACCEPTANCE_EVALUATED
            ),
            None,
        )
        outputs = [
            _tool_output(event_to_dict(event))
            for event in events
            if event.type is TaskEventType.TOOL_RESULT_RECORDED
        ]
        latest_failure = next(
            (
                checkpoint.failure.to_dict()
                for checkpoint in reversed(checkpoints)
                if checkpoint.failure is not None
            ),
            None,
        )
        return {
            "ok": True,
            "command": "result",
            "run_id": run_id,
            "result": {
                "run": run_to_dict(run),
                "progress": progress.to_dict(),
                "verified": (
                    progress.accepted is True and run.status is TaskRunStatus.COMPLETED
                ),
                "acceptance": (
                    dict(acceptance_event.payload)
                    if acceptance_event is not None
                    else None
                ),
                "outputs": outputs,
                "latest_failure": latest_failure,
                "pending_interactions": [
                    item.to_dict()
                    for item in self._long_task_store.list_pending_interactions(run_id)
                ],
                "artifacts": [
                    item.to_dict()
                    for item in self._long_task_store.list_artifacts(run_id)
                ],
                "side_effects": [
                    item.to_dict()
                    for item in self._long_task_store.list_side_effects(run_id)
                ],
            },
        }


def worker_tick_to_dict(tick: WorkerTickResult) -> dict[str, Any]:
    """Serialize one worker tick without exposing an active lease token."""

    lease = tick.lease.to_dict() if tick.lease is not None else None
    if lease is not None:
        lease.pop("lease_token", None)
    return {
        "ok": tick.status.value not in {"dead_letter", "lease_lost"},
        "command": "work",
        "worker_id": tick.worker_id,
        "run_id": tick.run_id,
        "status": tick.status.value,
        "reason": tick.reason,
        "lease": lease,
        "runtime_result": (
            runtime_result_to_dict(tick.runtime_result)
            if tick.runtime_result is not None
            else None
        ),
    }


def _tool_output(event: dict[str, Any]) -> dict[str, Any]:
    payload = dict(event.get("payload") or {})
    return {
        "event_id": event.get("id"),
        "seq": event.get("seq"),
        "created_at": event.get("created_at"),
        "step_id": payload.get("step_id"),
        "tool_name": payload.get("tool_name"),
        "result": payload.get("result"),
        "content": payload.get("content"),
        "error": payload.get("error"),
        "evidence": list(event.get("evidence") or []),
        "side_effects": list(event.get("side_effects") or []),
    }
