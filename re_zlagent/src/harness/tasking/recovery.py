"""Recovery policy for normalized tool failures."""

from __future__ import annotations

from dataclasses import dataclass

from harness.tools import (
    RecommendedNextAction,
    ToolErrorType,
    ToolResult,
    ToolResultStatus,
)

from .checkpoint import (
    Checkpoint,
    FailureDuration,
    FailureEnvelope,
    FailureType,
    FailureVisibility,
    RecoveryAction,
)
from .contract import TaskRunStatus


@dataclass(frozen=True, slots=True)
class ResumeDecision:
    """Decision about whether a checkpoint can be resumed automatically."""

    can_resume: bool
    action: RecoveryAction
    reason: str
    checkpoint_id: str
    failed_step: str | None = None


class RecoveryPolicy:
    """Map structured tool failures into task recovery envelopes."""

    _action_map = {
        RecommendedNextAction.RETRY: RecoveryAction.RETRY,
        RecommendedNextAction.ASK_USER: RecoveryAction.ASK_USER,
        RecommendedNextAction.READ_BEFORE_WRITE: RecoveryAction.READ_BEFORE_WRITE,
        RecommendedNextAction.USE_ALTERNATIVE_TOOL: RecoveryAction.USE_ALTERNATIVE_TOOL,
        RecommendedNextAction.MANUAL_REVIEW: RecoveryAction.MANUAL_REVIEW,
        RecommendedNextAction.STOP: RecoveryAction.STOP,
    }

    def recommend_from_tool_result(
        self,
        *,
        tool_name: str,
        result: ToolResult,
        failed_step: str | None = None,
        checkpoint_id: str | None = None,
    ) -> FailureEnvelope:
        if result.ok:
            raise ValueError("cannot create recovery envelope from successful tool result")

        action = self._action_map.get(
            result.recommended_next_action,
            RecoveryAction.MANUAL_REVIEW,
        )
        failure_type = self._failure_type(result)
        status = self._status(result, action)
        recoverable = self._recoverable(result, action)

        return FailureEnvelope(
            status=status,
            failed_step=failed_step or tool_name,
            failure_type=failure_type,
            root_cause=result.error or result.to_tool_message_content(),
            recoverable=recoverable,
            recommended_action=action,
            checkpoint_id=checkpoint_id,
            evidence=result.evidence,
            side_effects=result.side_effects,
            visibility=FailureVisibility.EXPLICIT,
            duration=self._duration(result),
            metadata={
                "tool_name": tool_name,
                "tool_status": result.status.value,
                "tool_error_type": result.error_type.value if result.error_type else None,
                "source": result.source,
            },
        )

    def _failure_type(self, result: ToolResult) -> FailureType:
        if result.error_type is ToolErrorType.PERMISSION_DENIED:
            return FailureType.PERMISSION_DENIED
        if result.error_type is ToolErrorType.UNSAFE_WRITE:
            return FailureType.UNSAFE_WRITE
        if result.status is ToolResultStatus.ERROR:
            return FailureType.TOOL_ERROR
        return FailureType.UNKNOWN

    def _duration(self, result: ToolResult) -> FailureDuration:
        if result.error_type in {
            ToolErrorType.EXTERNAL_UNAVAILABLE,
            ToolErrorType.TIMEOUT,
            ToolErrorType.RATE_LIMITED,
        }:
            return FailureDuration.TRANSIENT
        if result.error_type in {
            ToolErrorType.INVALID_INPUT,
            ToolErrorType.PERMISSION_DENIED,
            ToolErrorType.NOT_FOUND,
            ToolErrorType.UNSAFE_WRITE,
            ToolErrorType.CONFLICT,
        }:
            return FailureDuration.PERMANENT
        if result.status in {
            ToolResultStatus.DENIED,
            ToolResultStatus.REQUIRES_CONFIRMATION,
        }:
            return FailureDuration.PERMANENT
        return FailureDuration.UNKNOWN

    def _status(
        self,
        result: ToolResult,
        action: RecoveryAction,
    ) -> TaskRunStatus:
        if result.status is ToolResultStatus.REQUIRES_CONFIRMATION:
            return TaskRunStatus.WAITING_USER
        if action is RecoveryAction.STOP:
            return TaskRunStatus.FAILED
        if result.recoverable_by_model:
            return TaskRunStatus.RECOVERING
        if action is RecoveryAction.ASK_USER:
            return TaskRunStatus.WAITING_USER
        return TaskRunStatus.FAILED

    def _recoverable(
        self,
        result: ToolResult,
        action: RecoveryAction,
    ) -> bool:
        if action in {RecoveryAction.STOP, RecoveryAction.MANUAL_REVIEW}:
            return False
        if result.status is ToolResultStatus.REQUIRES_CONFIRMATION:
            return True
        return bool(result.recoverable_by_model)


class ResumePolicy:
    """Decide whether a failed checkpoint can be resumed automatically."""

    def decide(self, checkpoint: Checkpoint) -> ResumeDecision:
        failure = checkpoint.failure
        if failure is None:
            return ResumeDecision(
                can_resume=False,
                action=RecoveryAction.STOP,
                reason="checkpoint has no failure envelope",
                checkpoint_id=checkpoint.id,
            )
        if not failure.recoverable:
            return ResumeDecision(
                can_resume=False,
                action=failure.recommended_action,
                reason="failure is not marked recoverable",
                checkpoint_id=checkpoint.id,
                failed_step=failure.failed_step,
            )
        if failure.recommended_action is RecoveryAction.RETRY:
            return ResumeDecision(
                can_resume=True,
                action=RecoveryAction.RETRY,
                reason="retry failed step",
                checkpoint_id=checkpoint.id,
                failed_step=failure.failed_step,
            )
        reason_by_action = {
            RecoveryAction.ASK_USER: "requires user input before resume",
            RecoveryAction.READ_BEFORE_WRITE: "requires read-before-write before resume",
            RecoveryAction.USE_ALTERNATIVE_TOOL: "requires an alternative tool plan",
            RecoveryAction.MANUAL_REVIEW: "requires manual review before resume",
            RecoveryAction.STOP: "recovery action is stop",
        }
        return ResumeDecision(
            can_resume=False,
            action=failure.recommended_action,
            reason=reason_by_action.get(
                failure.recommended_action,
                "recovery action cannot be automated",
            ),
            checkpoint_id=checkpoint.id,
            failed_step=failure.failed_step,
        )
