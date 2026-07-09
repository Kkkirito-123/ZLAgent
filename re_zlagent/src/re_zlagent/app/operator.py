"""Operator-facing application service.

This layer is intentionally thin: it exposes app/operator commands while
delegating lifecycle mutation to RunControlService and progress reads to
TaskProgressReader.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from re_zlagent.harness.progress import (
    ProgressStatus,
    TaskProgressReader,
    TaskProgressSnapshot,
)
from re_zlagent.harness.runtime import (
    HarnessRuntime,
    RunControlResult,
    RunControlService,
    RuntimeAcceptanceInput,
    RuntimeResult,
)
from re_zlagent.harness.storage import TaskStore
from re_zlagent.harness.storage.serde import (
    checkpoint_to_dict,
    event_to_dict,
    run_to_dict,
)
from re_zlagent.harness.tasking import TaskEventType


@dataclass(frozen=True, slots=True)
class OperatorResponse:
    """Serializable app-level response for operator commands."""

    ok: bool
    command: str
    run_id: str
    progress: TaskProgressSnapshot | None = None
    result: RunControlResult | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-compatible response shape used by app surfaces."""

        data: dict[str, Any] = {
            "ok": self.ok,
            "command": self.command,
        }
        if self.run_id:
            data["run_id"] = self.run_id
        if self.progress is not None:
            data["progress"] = self.progress.to_dict()
        if self.result is not None:
            data["result"] = control_result_to_dict(self.result)
        if self.metadata:
            data["metadata"] = dict(self.metadata)
        return data


@dataclass(frozen=True, slots=True)
class ApprovalResponse:
    """Serializable app-level response for user approval recovery."""

    ok: bool
    command: str
    run_id: str
    checkpoint_id: str | None = None
    result: RuntimeResult | None = None
    error: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", dict(self.metadata))
        if self.error is not None:
            object.__setattr__(self, "error", dict(self.error))

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-compatible approval response shape."""

        data: dict[str, Any] = {
            "ok": self.ok,
            "command": self.command,
            "run_id": self.run_id,
            "checkpoint_id": self.checkpoint_id,
        }
        if self.result is not None:
            data["result"] = runtime_result_to_dict(self.result)
        if self.error is not None:
            data["error"] = dict(self.error)
        if self.metadata:
            data["metadata"] = dict(self.metadata)
        return data


class OperatorService:
    """App boundary for read-only progress and operator run controls."""

    def __init__(
        self,
        store: TaskStore,
        *,
        progress_reader: TaskProgressReader | None = None,
        controls: RunControlService | None = None,
    ) -> None:
        self._progress_reader = progress_reader or TaskProgressReader(store)
        self._controls = controls or RunControlService(store)

    def status(self, run_id: str) -> OperatorResponse:
        """Read polling-friendly run progress without mutating state."""

        snapshot = self._progress_reader.snapshot(run_id)
        return OperatorResponse(
            ok=snapshot.status is not ProgressStatus.MISSING,
            command="status",
            run_id=run_id,
            progress=snapshot,
        )

    def pause(
        self,
        run_id: str,
        *,
        reason: str = "",
        actor: str = "user",
    ) -> OperatorResponse:
        """Pause a run through the runtime control service."""

        result = self._controls.pause(run_id, reason=reason, actor=actor)
        return self._from_control("pause", run_id, result)

    def resume(
        self,
        run_id: str,
        *,
        feedback: str = "",
        actor: str = "user",
    ) -> OperatorResponse:
        """Resume a paused or user-blocked run through runtime controls."""

        result = self._controls.resume(run_id, feedback=feedback, actor=actor)
        return self._from_control("resume", run_id, result)

    def cancel(
        self,
        run_id: str,
        *,
        reason: str = "",
        actor: str = "user",
    ) -> OperatorResponse:
        """Cancel a non-terminal run through runtime controls."""

        result = self._controls.cancel(run_id, reason=reason, actor=actor)
        return self._from_control("cancel", run_id, result)

    def fork(
        self,
        run_id: str,
        *,
        new_run_id: str,
        reason: str = "",
        actor: str = "user",
        checkpoint_id: str | None = None,
    ) -> OperatorResponse:
        """Fork a run while preserving source lineage through runtime controls."""

        result = self._controls.fork(
            run_id,
            new_run_id=new_run_id,
            reason=reason,
            actor=actor,
            checkpoint_id=checkpoint_id,
        )
        return self._from_control("fork", run_id, result)

    @staticmethod
    def _from_control(
        command: str,
        run_id: str,
        result: RunControlResult,
    ) -> OperatorResponse:
        return OperatorResponse(
            ok=result.accepted,
            command=command,
            run_id=run_id,
            result=result,
        )


class ApprovalService:
    """App boundary for explicit user approval recovery."""

    def __init__(self, runtime: HarnessRuntime) -> None:
        self._runtime = runtime

    async def approve_checkpoint(
        self,
        run_id: str,
        *,
        checkpoint_id: str | None = None,
        feedback: str = "",
        acceptance: RuntimeAcceptanceInput | None = None,
        interactive: bool = True,
    ) -> ApprovalResponse:
        """Approve and resume a waiting-user checkpoint through runtime."""

        try:
            result = await self._runtime.resume_with_user_approval(
                run_id=run_id,
                checkpoint_id=checkpoint_id,
                feedback=feedback,
                acceptance=acceptance,
                interactive=interactive,
            )
        except ValueError as exc:
            return ApprovalResponse(
                ok=False,
                command="approve_checkpoint",
                run_id=run_id,
                checkpoint_id=checkpoint_id,
                error={
                    "type": "value_error",
                    "message": str(exc),
                },
            )
        return ApprovalResponse(
            ok=True,
            command="approve_checkpoint",
            run_id=run_id,
            checkpoint_id=_approved_checkpoint_id(
                result,
                fallback=checkpoint_id,
            ),
            result=result,
            metadata={
                "current_checkpoint_id": result.run.current_checkpoint_id,
            },
        )


def control_result_to_dict(result: RunControlResult) -> dict[str, Any]:
    """Serialize a RunControlResult without leaking storage internals."""

    return {
        "accepted": result.accepted,
        "action": result.action.value,
        "reason": result.reason,
        "run": run_to_dict(result.run),
        "event": event_to_dict(result.event) if result.event else None,
        "checkpoint": (
            checkpoint_to_dict(result.checkpoint)
            if result.checkpoint
            else None
        ),
        "forked_run": (
            run_to_dict(result.forked_run)
            if result.forked_run
            else None
        ),
        "metadata": dict(result.metadata),
    }


def runtime_result_to_dict(result: RuntimeResult) -> dict[str, Any]:
    """Serialize a RuntimeResult for app/operator surfaces."""

    return {
        "accepted": result.accepted,
        "run": run_to_dict(result.run),
        "events": [event_to_dict(event) for event in result.events],
        "checkpoints": [
            checkpoint_to_dict(checkpoint)
            for checkpoint in result.checkpoints
        ],
        "tool_results": [
            {
                "metadata": tool_result.to_metadata(),
                "content": tool_result.content,
                "error": tool_result.error,
                "evidence": [
                    item.to_dict()
                    for item in tool_result.evidence
                ],
                "side_effects": [
                    item.to_dict()
                    for item in tool_result.side_effects
                ],
            }
            for tool_result in result.tool_results
        ],
        "acceptance_decision": (
            _acceptance_decision_to_dict(result.acceptance_decision)
            if result.acceptance_decision is not None
            else None
        ),
        "failure": result.failure.to_dict() if result.failure else None,
        "step_verifications": [
            verification.to_dict()
            for verification in result.step_verifications
        ],
    }


def _acceptance_decision_to_dict(decision: Any) -> dict[str, Any]:
    return {
        "accepted": decision.accepted,
        "status": decision.status.value,
        "criteria": {
            key: value.value
            for key, value in decision.criteria.items()
        },
        "passed_criteria": list(decision.passed_criteria),
        "failed_criteria": list(decision.failed_criteria),
        "blocked_criteria": list(decision.blocked_criteria),
        "optional_failed_criteria": list(decision.optional_failed_criteria),
        "reason": decision.reason,
        "evidence_refs": list(decision.evidence_refs),
    }


def _approved_checkpoint_id(
    result: RuntimeResult,
    *,
    fallback: str | None,
) -> str | None:
    for event in result.events:
        if event.type is not TaskEventType.USER_INPUT_RECORDED:
            continue
        checkpoint_id = event.payload.get("checkpoint_id")
        if isinstance(checkpoint_id, str):
            return checkpoint_id
    return fallback
