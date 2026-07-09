"""Operator-facing application service.

This layer is intentionally thin: it exposes app/operator commands while
delegating lifecycle mutation to RunControlService and progress reads to
TaskProgressReader.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from re_zlagent.harness.progress import ProgressStatus, TaskProgressReader, TaskProgressSnapshot
from re_zlagent.harness.runtime import RunControlResult, RunControlService
from re_zlagent.harness.storage import TaskStore
from re_zlagent.harness.storage.serde import checkpoint_to_dict, event_to_dict, run_to_dict


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
