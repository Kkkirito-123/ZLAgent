from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from re_zlagent.harness.runtime import (  # noqa: E402
    HarnessRuntime,
    RuntimeAcceptanceFacts,
    RuntimeToolStep,
)
from re_zlagent.harness.storage import InMemoryTaskStore  # noqa: E402
from re_zlagent.harness.tasking import (  # noqa: E402
    AcceptanceCriterion,
    AcceptanceStatus,
    FailureDuration,
    FailureVisibility,
    CriterionType,
    TaskContract,
    TaskEventType,
    TaskRun,
    TaskRunStatus,
    StepStatus,
)
from re_zlagent.harness.tools import (  # noqa: E402
    Evidence,
    RecommendedNextAction,
    Tool,
    ToolErrorType,
    ToolPermission,
    ToolRegistry,
    ToolResult,
)


class EchoTool(Tool):
    name = "echo"
    description = "Return a deterministic evidence ref."
    permission = ToolPermission.SAFE
    input_schema = {
        "type": "object",
        "properties": {"ref": {"type": "string"}},
        "required": ["ref"],
    }

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        ref = arguments.get("ref", "tool:echo")
        return ToolResult.success(
            f"observed {ref}",
            evidence=[Evidence(type="runtime", ref=ref, summary="echo evidence")],
            source=self.name,
        )


class FailingTool(Tool):
    name = "fail"
    description = "Return a recoverable deterministic failure."
    permission = ToolPermission.SAFE

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        return ToolResult.failure(
            "temporary failure",
            error_type=ToolErrorType.EXTERNAL_UNAVAILABLE,
            recoverable_by_model=True,
            recommended_next_action=RecommendedNextAction.RETRY,
            source=self.name,
        )


class NeedsAlternativeTool(Tool):
    name = "needs_alternative"
    description = "Fail with explicit alternative-tool guidance."
    permission = ToolPermission.SAFE

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        return ToolResult.failure(
            "primary tool unavailable",
            error_type=ToolErrorType.EXTERNAL_UNAVAILABLE,
            recoverable_by_model=True,
            recommended_next_action=RecommendedNextAction.USE_ALTERNATIVE_TOOL,
            source=self.name,
        )


class FailOnceTool(Tool):
    name = "fail_once"
    description = "Fail once with retry guidance, then return evidence."
    permission = ToolPermission.SAFE

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        self.calls += 1
        if self.calls == 1:
            evidence = []
            if arguments.get("fail_ref"):
                evidence.append(
                    Evidence(
                        type="runtime",
                        ref=arguments["fail_ref"],
                        summary="failed-call evidence",
                    )
                )
            return ToolResult.failure(
                "temporary failure",
                error_type=ToolErrorType.EXTERNAL_UNAVAILABLE,
                recoverable_by_model=True,
                recommended_next_action=RecommendedNextAction.RETRY,
                evidence=evidence,
                source=self.name,
            )
        ref = arguments.get("ref", "tool:retry")
        return ToolResult.success(
            f"retried {ref}",
            evidence=[Evidence(type="runtime", ref=ref, summary="retry evidence")],
            source=self.name,
        )


class ConfirmTool(Tool):
    name = "confirm_write"
    description = "Confirm-tier fake mutation."
    permission = ToolPermission.CONFIRM
    is_read_only = False

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        return ToolResult.success(
            "confirmed",
            evidence=[
                Evidence(
                    type="runtime",
                    ref="confirm:evidence",
                    summary="confirm evidence",
                )
            ],
            source=self.name,
        )


class RuntimeLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def _contract(self, *criteria: AcceptanceCriterion) -> TaskContract:
        return TaskContract(
            id="contract-1",
            user_goal="run deterministic lifecycle",
            acceptance_criteria=criteria
            or (
                AcceptanceCriterion(
                    id="echo-evidence",
                    description="echo evidence exists",
                    type=CriterionType.TOOL_EVIDENCE,
                    evidence_refs=("tool:echo",),
                ),
            ),
        )

    def _runtime(self) -> tuple[HarnessRuntime, InMemoryTaskStore]:
        store = InMemoryTaskStore()
        registry = ToolRegistry()
        registry.register(EchoTool())
        registry.register(FailingTool())
        registry.register(NeedsAlternativeTool())
        registry.register(FailOnceTool())
        registry.register(ConfirmTool())
        return HarnessRuntime(store=store, tools=registry), store

    async def test_successful_run_records_events_checkpoints_and_completion(self) -> None:
        runtime, store = self._runtime()

        result = await runtime.run(
            contract=self._contract(),
            run_id="run-1",
            steps=[RuntimeToolStep(id="step-1", tool_name="echo", arguments={"ref": "tool:echo"})],
        )

        self.assertTrue(result.accepted)
        self.assertEqual(result.run.status, TaskRunStatus.COMPLETED)
        self.assertEqual(result.acceptance_decision.status, AcceptanceStatus.PASSED)
        self.assertEqual(store.latest_checkpoint("run-1").status.value, "completed")
        self.assertIn(TaskEventType.RUN_CREATED, [event.type for event in result.events])
        self.assertIn(TaskEventType.PLAN_STEP_STARTED, [event.type for event in result.events])
        self.assertIn(TaskEventType.PLAN_STEP_VERIFIED, [event.type for event in result.events])
        self.assertIn(TaskEventType.TOOL_RESULT_RECORDED, [event.type for event in result.events])
        self.assertIn(TaskEventType.ACCEPTANCE_EVALUATED, [event.type for event in result.events])
        self.assertIn(TaskEventType.RUN_COMPLETED, [event.type for event in result.events])
        self.assertEqual(result.run.event_seq, result.events[-1].seq)
        self.assertEqual(result.step_verifications[0].step_id, "step-1")
        self.assertEqual(result.step_verifications[0].status, StepStatus.PASSED)

    async def test_step_verification_failure_stops_before_acceptance(self) -> None:
        runtime, store = self._runtime()

        result = await runtime.run(
            contract=self._contract(),
            run_id="run-1",
            steps=[
                RuntimeToolStep(
                    id="step-1",
                    tool_name="echo",
                    arguments={"ref": "tool:echo"},
                    required_evidence_refs=("tool:missing",),
                )
            ],
        )

        self.assertFalse(result.accepted)
        self.assertEqual(result.run.status, TaskRunStatus.FAILED)
        self.assertIsNotNone(result.failure)
        self.assertEqual(result.failure.failure_type.value, "step_verification_failed")
        self.assertEqual(result.failure.visibility, FailureVisibility.IMPLICIT)
        self.assertEqual(result.failure.duration, FailureDuration.PERMANENT)
        self.assertEqual(result.step_verifications[0].status, StepStatus.FAILED)
        self.assertEqual(
            result.step_verifications[0].missing_evidence_refs,
            ("tool:missing",),
        )
        self.assertNotIn(
            TaskEventType.ACCEPTANCE_EVALUATED,
            [event.type for event in result.events],
        )
        self.assertEqual(store.latest_checkpoint("run-1").status.value, "failed")

    async def test_tool_failure_creates_failure_checkpoint_and_stops(self) -> None:
        runtime, store = self._runtime()

        result = await runtime.run(
            contract=self._contract(),
            run_id="run-1",
            steps=[RuntimeToolStep(id="step-1", tool_name="fail")],
        )

        self.assertFalse(result.accepted)
        self.assertEqual(result.run.status, TaskRunStatus.RECOVERING)
        self.assertIsNotNone(result.failure)
        self.assertTrue(result.failure.recoverable)
        self.assertEqual(store.latest_checkpoint("run-1").failure.failed_step, "step-1")
        self.assertIn(TaskEventType.RUN_FAILED, [event.type for event in result.events])
        self.assertEqual(result.step_verifications[0].status, StepStatus.FAILED)

    async def test_resume_retry_from_failed_checkpoint_can_complete(self) -> None:
        runtime, store = self._runtime()

        first = await runtime.run(
            contract=self._contract(
                AcceptanceCriterion(
                    id="retry-evidence",
                    description="retry evidence exists",
                    type=CriterionType.TOOL_EVIDENCE,
                    evidence_refs=("tool:retry",),
                )
            ),
            run_id="run-1",
            steps=[
                RuntimeToolStep(
                    id="step-1",
                    tool_name="fail_once",
                    arguments={"ref": "tool:retry"},
                )
            ],
        )
        self.assertEqual(first.run.status, TaskRunStatus.RECOVERING)

        resumed = await runtime.resume_from_checkpoint(run_id="run-1")

        self.assertTrue(resumed.accepted)
        self.assertEqual(resumed.run.status, TaskRunStatus.COMPLETED)
        self.assertEqual(resumed.acceptance_decision.status, AcceptanceStatus.PASSED)
        self.assertEqual(store.latest_checkpoint("run-1").status.value, "completed")
        self.assertEqual(resumed.step_verifications[-1].status, StepStatus.PASSED)
        tool_events = [
            event
            for event in resumed.events
            if event.type is TaskEventType.TOOL_RESULT_RECORDED
        ]
        self.assertEqual(len(tool_events), 2)
        self.assertIn("resume", tool_events[-1].payload)
        self.assertEqual(
            tool_events[-1].payload["resume"]["source_event_id"],
            tool_events[0].id,
        )

    async def test_resume_with_alternative_tool_can_complete(self) -> None:
        runtime, store = self._runtime()

        first = await runtime.run(
            contract=self._contract(
                AcceptanceCriterion(
                    id="alternative-evidence",
                    description="alternative evidence exists",
                    type=CriterionType.TOOL_EVIDENCE,
                    evidence_refs=("tool:alternative",),
                )
            ),
            run_id="run-1",
            steps=[
                RuntimeToolStep(
                    id="step-1",
                    tool_name="needs_alternative",
                )
            ],
        )
        self.assertEqual(first.run.status, TaskRunStatus.RECOVERING)

        resumed = await runtime.resume_with_alternative_tool(
            run_id="run-1",
            alternative_step=RuntimeToolStep(
                id="alt-step",
                tool_name="echo",
                arguments={"ref": "tool:alternative"},
                required_evidence_refs=("tool:alternative",),
            ),
        )

        self.assertTrue(resumed.accepted)
        self.assertEqual(resumed.run.status, TaskRunStatus.COMPLETED)
        self.assertEqual(store.latest_checkpoint("run-1").status.value, "completed")
        self.assertEqual(resumed.step_verifications[-1].step_id, "step-1")
        self.assertEqual(resumed.step_verifications[-1].status, StepStatus.PASSED)
        tool_events = [
            event
            for event in resumed.events
            if event.type is TaskEventType.TOOL_RESULT_RECORDED
        ]
        self.assertIn("alternative", tool_events[-1].payload)
        self.assertEqual(
            tool_events[-1].payload["alternative"]["failed_step"],
            "step-1",
        )

    async def test_alternative_resume_rejects_non_alternative_checkpoint(self) -> None:
        runtime, _ = self._runtime()

        await runtime.run(
            contract=self._contract(),
            run_id="run-1",
            steps=[RuntimeToolStep(id="step-1", tool_name="fail")],
        )

        with self.assertRaises(ValueError) as context:
            await runtime.resume_with_alternative_tool(
                run_id="run-1",
                alternative_step=RuntimeToolStep(
                    id="alt-step",
                    tool_name="echo",
                ),
            )

        self.assertIn("not waiting for alternative tool", str(context.exception))

    async def test_alternative_resume_failure_creates_checkpoint(self) -> None:
        runtime, store = self._runtime()

        await runtime.run(
            contract=self._contract(),
            run_id="run-1",
            steps=[RuntimeToolStep(id="step-1", tool_name="needs_alternative")],
        )
        source_checkpoint = store.latest_checkpoint("run-1")

        result = await runtime.resume_with_alternative_tool(
            run_id="run-1",
            alternative_step=RuntimeToolStep(
                id="alt-step",
                tool_name="fail",
            ),
        )
        latest = store.latest_checkpoint("run-1")

        self.assertFalse(result.accepted)
        self.assertEqual(result.run.status, TaskRunStatus.RECOVERING)
        self.assertEqual(result.failure.failed_step, "step-1")
        self.assertEqual(
            latest.state["alternative_of_checkpoint_id"],
            source_checkpoint.id,
        )
        self.assertEqual(latest.failure.recommended_action.value, "retry")

    async def test_resume_requires_existing_checkpoint(self) -> None:
        runtime, store = self._runtime()
        store.save_contract(self._contract())
        store.create_run(TaskRun(id="run-1", contract_id="contract-1"))

        with self.assertRaises(ValueError):
            await runtime.resume_from_checkpoint(run_id="run-1")

    async def test_resume_non_retry_failure_waits_for_user(self) -> None:
        runtime, _ = self._runtime()

        await runtime.run(
            contract=self._contract(),
            run_id="run-1",
            steps=[RuntimeToolStep(id="step-1", tool_name="confirm_write")],
        )

        resumed = await runtime.resume_from_checkpoint(run_id="run-1")

        self.assertFalse(resumed.accepted)
        self.assertEqual(resumed.run.status, TaskRunStatus.WAITING_USER)
        self.assertEqual(resumed.tool_results, ())
        self.assertIsNotNone(resumed.failure)
        self.assertIn(
            TaskEventType.USER_INPUT_REQUIRED,
            [event.type for event in resumed.events],
        )

    async def test_resume_retry_still_requires_acceptance(self) -> None:
        runtime, store = self._runtime()

        await runtime.run(
            contract=self._contract(
                AcceptanceCriterion(
                    id="missing-evidence",
                    description="missing evidence",
                    type=CriterionType.TOOL_EVIDENCE,
                    evidence_refs=("tool:missing",),
                )
            ),
            run_id="run-1",
            steps=[
                RuntimeToolStep(
                    id="step-1",
                    tool_name="fail_once",
                    arguments={"ref": "tool:retry"},
                )
            ],
        )

        resumed = await runtime.resume_from_checkpoint(run_id="run-1")

        self.assertFalse(resumed.accepted)
        self.assertEqual(resumed.run.status, TaskRunStatus.ACCEPTANCE_FAILED)
        self.assertEqual(resumed.acceptance_decision.status, AcceptanceStatus.FAILED)
        self.assertEqual(store.latest_checkpoint("run-1").status.value, "acceptance_failed")

    async def test_resume_ignores_failed_tool_evidence_for_acceptance(self) -> None:
        runtime, store = self._runtime()

        await runtime.run(
            contract=self._contract(
                AcceptanceCriterion(
                    id="failed-call-evidence",
                    description="failed call evidence must not complete task",
                    type=CriterionType.TOOL_EVIDENCE,
                    evidence_refs=("tool:failed",),
                )
            ),
            run_id="run-1",
            steps=[
                RuntimeToolStep(
                    id="step-1",
                    tool_name="fail_once",
                    arguments={
                        "ref": "tool:retry",
                        "fail_ref": "tool:failed",
                    },
                )
            ],
        )

        resumed = await runtime.resume_from_checkpoint(run_id="run-1")

        self.assertFalse(resumed.accepted)
        self.assertEqual(resumed.run.status, TaskRunStatus.ACCEPTANCE_FAILED)
        self.assertEqual(resumed.acceptance_decision.status, AcceptanceStatus.FAILED)
        self.assertEqual(store.latest_checkpoint("run-1").status.value, "acceptance_failed")

    async def test_confirm_tier_tool_waits_for_user_without_allow_confirm(self) -> None:
        runtime, store = self._runtime()

        result = await runtime.run(
            contract=self._contract(),
            run_id="run-1",
            steps=[RuntimeToolStep(id="step-1", tool_name="confirm_write")],
        )

        self.assertFalse(result.accepted)
        self.assertEqual(result.run.status, TaskRunStatus.WAITING_USER)
        self.assertEqual(
            store.latest_checkpoint("run-1").failure.failed_step,
            "step-1",
        )
        self.assertIn(TaskEventType.USER_INPUT_REQUIRED, [event.type for event in result.events])

    async def test_user_approval_resume_executes_confirm_tool_and_rechecks_acceptance(self) -> None:
        runtime, store = self._runtime()

        await runtime.run(
            contract=self._contract(
                AcceptanceCriterion(
                    id="confirm-evidence",
                    description="confirm evidence exists",
                    type=CriterionType.TOOL_EVIDENCE,
                    evidence_refs=("confirm:evidence",),
                )
            ),
            run_id="run-1",
            steps=[
                RuntimeToolStep(
                    id="step-1",
                    tool_name="confirm_write",
                    required_evidence_refs=("confirm:evidence",),
                )
            ],
        )

        approved = await runtime.resume_with_user_approval(
            run_id="run-1",
            feedback="approved",
        )

        self.assertTrue(approved.accepted)
        self.assertEqual(approved.run.status, TaskRunStatus.COMPLETED)
        self.assertIn(
            TaskEventType.USER_INPUT_RECORDED,
            [event.type for event in approved.events],
        )
        self.assertEqual(approved.step_verifications[-1].status, StepStatus.PASSED)
        self.assertEqual(store.latest_checkpoint("run-1").status.value, "completed")

    async def test_runtime_rejects_out_of_order_step_dependencies(self) -> None:
        runtime, _ = self._runtime()

        with self.assertRaises(ValueError):
            await runtime.run(
                contract=self._contract(),
                run_id="run-1",
                steps=[
                    RuntimeToolStep(
                        id="step-2",
                        tool_name="echo",
                        depends_on=("step-1",),
                    ),
                    RuntimeToolStep(id="step-1", tool_name="echo"),
                ],
            )

    async def test_acceptance_failure_creates_acceptance_failed_run(self) -> None:
        runtime, store = self._runtime()

        result = await runtime.run(
            contract=self._contract(
                AcceptanceCriterion(
                    id="missing-evidence",
                    description="missing evidence",
                    type=CriterionType.TOOL_EVIDENCE,
                    evidence_refs=("tool:missing",),
                )
            ),
            run_id="run-1",
            steps=[RuntimeToolStep(id="step-1", tool_name="echo", arguments={"ref": "tool:echo"})],
        )

        self.assertFalse(result.accepted)
        self.assertEqual(result.run.status, TaskRunStatus.ACCEPTANCE_FAILED)
        self.assertIsNotNone(result.failure)
        self.assertEqual(result.acceptance_decision.status, AcceptanceStatus.FAILED)
        self.assertEqual(store.latest_checkpoint("run-1").status.value, "acceptance_failed")
        self.assertEqual(result.step_verifications[0].status, StepStatus.PASSED)

    async def test_acceptance_blocked_waits_for_user(self) -> None:
        runtime, _ = self._runtime()

        result = await runtime.run(
            contract=self._contract(
                AcceptanceCriterion(
                    id="approval",
                    description="needs user approval",
                    type=CriterionType.HUMAN_APPROVAL,
                )
            ),
            run_id="run-1",
            steps=[],
        )

        self.assertFalse(result.accepted)
        self.assertEqual(result.run.status, TaskRunStatus.WAITING_USER)
        self.assertEqual(result.acceptance_decision.status, AcceptanceStatus.BLOCKED)
        self.assertIn(TaskEventType.USER_INPUT_REQUIRED, [event.type for event in result.events])

    async def test_acceptance_input_can_supply_tests_and_manual_evidence(self) -> None:
        runtime, _ = self._runtime()

        result = await runtime.run(
            contract=self._contract(
                AcceptanceCriterion(
                    id="manual-evidence",
                    description="manual evidence exists",
                    type=CriterionType.TOOL_EVIDENCE,
                    evidence_refs=("manual:evidence",),
                ),
                AcceptanceCriterion(
                    id="unit-tests",
                    description="unit tests pass",
                    type=CriterionType.TEST_RESULT,
                ),
            ),
            run_id="run-1",
            steps=[],
            acceptance_facts=RuntimeAcceptanceFacts(
                evidence_refs=("manual:evidence",),
                passed_tests=("unit-tests",),
            ),
        )

        self.assertTrue(result.accepted)
        self.assertEqual(result.run.status, TaskRunStatus.COMPLETED)

    def test_store_update_run_does_not_decrease_event_seq(self) -> None:
        store = InMemoryTaskStore()
        store.save_contract(self._contract())
        store.create_run(TaskRun(id="run-1", contract_id="contract-1"))
        store.append_event(run_id="run-1", type=TaskEventType.RUN_CREATED)

        stale = TaskRun(
            id="run-1",
            contract_id="contract-1",
            status=TaskRunStatus.RUNNING,
            event_seq=0,
        )
        updated = store.update_run(stale)

        self.assertEqual(updated.event_seq, 1)
        self.assertEqual(store.get_run("run-1").event_seq, 1)


if __name__ == "__main__":
    unittest.main()
