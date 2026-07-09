from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from harness.tasking import (  # noqa: E402
    Checkpoint,
    CheckpointStatus,
    FailureEnvelope,
    FailureDuration,
    FailureType,
    FailureVisibility,
    RecoveryAction,
    RecoveryPolicy,
    ResumePolicy,
    TaskRunStatus,
)
from harness.tools import (  # noqa: E402
    RecommendedNextAction,
    ToolErrorType,
    ToolResult,
    ToolResultStatus,
)


class RecoveryPolicyTests(unittest.TestCase):
    def _checkpoint(
        self,
        *,
        failure: FailureEnvelope | None,
        status: CheckpointStatus = CheckpointStatus.FAILED,
    ) -> Checkpoint:
        return Checkpoint(
            id="chk-1",
            run_id="run-1",
            seq=1,
            status=status,
            resume_from_event_id="evt-1",
            failure=failure,
        )

    def _failure(
        self,
        *,
        recoverable: bool,
        action: RecoveryAction,
    ) -> FailureEnvelope:
        return FailureEnvelope(
            status=TaskRunStatus.RECOVERING if recoverable else TaskRunStatus.FAILED,
            failed_step="step-1",
            failure_type=FailureType.TOOL_ERROR,
            root_cause="temporary failure",
            recoverable=recoverable,
            recommended_action=action,
        )

    def test_unsafe_write_maps_to_read_before_write_recovery(self) -> None:
        result = ToolResult.failure(
            "fresh read required",
            error_type=ToolErrorType.UNSAFE_WRITE,
            recoverable_by_model=True,
            recommended_next_action=RecommendedNextAction.READ_BEFORE_WRITE,
        )

        envelope = RecoveryPolicy().recommend_from_tool_result(
            tool_name="write_file",
            result=result,
            checkpoint_id="chk-1",
        )

        self.assertEqual(envelope.status, TaskRunStatus.RECOVERING)
        self.assertEqual(envelope.failure_type, FailureType.UNSAFE_WRITE)
        self.assertTrue(envelope.recoverable)
        self.assertEqual(envelope.recommended_action, RecoveryAction.READ_BEFORE_WRITE)
        self.assertEqual(envelope.checkpoint_id, "chk-1")
        self.assertEqual(envelope.visibility, FailureVisibility.EXPLICIT)
        self.assertEqual(envelope.duration, FailureDuration.PERMANENT)
        self.assertEqual(envelope.perturbation_class, "explicit_permanent")

    def test_requires_confirmation_waits_for_user(self) -> None:
        result = ToolResult.requires_confirmation("needs approval")

        envelope = RecoveryPolicy().recommend_from_tool_result(
            tool_name="write_file",
            result=result,
        )

        self.assertEqual(envelope.status, TaskRunStatus.WAITING_USER)
        self.assertEqual(envelope.failure_type, FailureType.PERMISSION_DENIED)
        self.assertTrue(envelope.recoverable)
        self.assertEqual(envelope.recommended_action, RecoveryAction.ASK_USER)
        self.assertEqual(envelope.perturbation_class, "explicit_permanent")

    def test_transient_tool_errors_are_classified(self) -> None:
        result = ToolResult.failure(
            "try later",
            error_type=ToolErrorType.TIMEOUT,
            recoverable_by_model=True,
            recommended_next_action=RecommendedNextAction.RETRY,
        )

        envelope = RecoveryPolicy().recommend_from_tool_result(
            tool_name="read_url",
            result=result,
        )

        self.assertEqual(envelope.duration, FailureDuration.TRANSIENT)
        self.assertEqual(envelope.perturbation_class, "explicit_transient")

    def test_stop_action_is_not_recoverable(self) -> None:
        result = ToolResult.denied("blocked")

        envelope = RecoveryPolicy().recommend_from_tool_result(
            tool_name="dangerous_tool",
            result=result,
        )

        self.assertEqual(envelope.status, TaskRunStatus.FAILED)
        self.assertFalse(envelope.recoverable)
        self.assertEqual(envelope.recommended_action, RecoveryAction.STOP)

    def test_successful_result_cannot_create_failure_envelope(self) -> None:
        with self.assertRaises(ValueError):
            RecoveryPolicy().recommend_from_tool_result(
                tool_name="read_file",
                result=ToolResult.success("ok"),
            )

    def test_unknown_error_defaults_to_manual_review(self) -> None:
        result = ToolResult(
            ok=False,
            content="",
            error="bad state",
            status=ToolResultStatus.ERROR,
            error_type=ToolErrorType.UNKNOWN,
        )

        envelope = RecoveryPolicy().recommend_from_tool_result(
            tool_name="unknown_tool",
            result=result,
        )

        self.assertEqual(envelope.status, TaskRunStatus.FAILED)
        self.assertEqual(envelope.failure_type, FailureType.TOOL_ERROR)
        self.assertEqual(envelope.recommended_action, RecoveryAction.MANUAL_REVIEW)

    def test_resume_policy_allows_recoverable_retry_checkpoint(self) -> None:
        decision = ResumePolicy().decide(
            self._checkpoint(
                failure=self._failure(
                    recoverable=True,
                    action=RecoveryAction.RETRY,
                )
            )
        )

        self.assertTrue(decision.can_resume)
        self.assertEqual(decision.action, RecoveryAction.RETRY)
        self.assertEqual(decision.failed_step, "step-1")

    def test_resume_policy_rejects_checkpoint_without_failure(self) -> None:
        decision = ResumePolicy().decide(
            self._checkpoint(
                failure=None,
                status=CheckpointStatus.RUNNING,
            )
        )

        self.assertFalse(decision.can_resume)
        self.assertEqual(decision.action, RecoveryAction.STOP)

    def test_resume_policy_rejects_user_input_recovery(self) -> None:
        decision = ResumePolicy().decide(
            self._checkpoint(
                failure=self._failure(
                    recoverable=True,
                    action=RecoveryAction.ASK_USER,
                )
            )
        )

        self.assertFalse(decision.can_resume)
        self.assertEqual(decision.action, RecoveryAction.ASK_USER)
        self.assertIn("user input", decision.reason)


if __name__ == "__main__":
    unittest.main()
