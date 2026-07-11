from __future__ import annotations

import sys
import tempfile
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
from re_zlagent.harness.storage import (  # noqa: E402
    InMemoryLongTaskStore,
    InMemoryTaskStore,
    SqliteTaskStore,
)
from re_zlagent.harness.tasking import (  # noqa: E402
    AcceptanceCriterion,
    CriterionType,
    InteractionStatus,
    StepStatus,
    TaskContract,
    TaskEventType,
    TaskRunStatus,
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


class RecordingEchoTool(Tool):
    name = "recording_echo"
    description = "Record calls and return deterministic evidence."
    permission = ToolPermission.SAFE

    def __init__(self) -> None:
        self.refs: list[str] = []

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        ref = str(arguments["ref"])
        self.refs.append(ref)
        return ToolResult.success(
            f"observed {ref}",
            evidence=[Evidence(type="runtime", ref=ref)],
            source=self.name,
        )


class ConfiguredRetryTool(Tool):
    name = "retry_stage"
    description = "Fail a configured number of calls before succeeding."
    permission = ToolPermission.SAFE

    def __init__(self, *, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        self.calls += 1
        if self.calls <= self.failures:
            return ToolResult.failure(
                "temporary stage failure",
                error_type=ToolErrorType.EXTERNAL_UNAVAILABLE,
                recoverable_by_model=True,
                recommended_next_action=RecommendedNextAction.RETRY,
                source=self.name,
            )
        ref = str(arguments["ref"])
        return ToolResult.success(
            f"recovered {ref}",
            evidence=[Evidence(type="runtime", ref=ref)],
            source=self.name,
        )


class AlternativeRequiredTool(Tool):
    name = "alternative_required"
    description = "Require a caller-supplied alternative capability."
    permission = ToolPermission.SAFE

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        return ToolResult.failure(
            "primary capability unavailable",
            error_type=ToolErrorType.EXTERNAL_UNAVAILABLE,
            recoverable_by_model=True,
            recommended_next_action=RecommendedNextAction.USE_ALTERNATIVE_TOOL,
            source=self.name,
        )


class ConfirmMutationTool(Tool):
    name = "confirm_mutation"
    description = "A confirm-tier test mutation."
    permission = ToolPermission.CONFIRM
    is_read_only = False

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        return ToolResult.success(
            "confirmed",
            evidence=[Evidence(type="runtime", ref="evidence:confirmed")],
            source=self.name,
        )


class RuntimeContinuationTests(unittest.IsolatedAsyncioTestCase):
    def _contract(
        self,
        *,
        contract_id: str = "contract-1",
        include_test: bool = False,
    ) -> TaskContract:
        criteria = [
            AcceptanceCriterion(
                id="final-evidence",
                description="the final stage produced evidence",
                type=CriterionType.TOOL_EVIDENCE,
                evidence_refs=("evidence:step-3",),
            )
        ]
        if include_test:
            criteria.append(
                AcceptanceCriterion(
                    id="trusted-tests",
                    description="trusted tests passed",
                    type=CriterionType.TEST_RESULT,
                )
            )
        return TaskContract(
            id=contract_id,
            user_goal="complete every persisted plan step",
            acceptance_criteria=tuple(criteria),
        )

    @staticmethod
    def _three_steps() -> tuple[RuntimeToolStep, ...]:
        return (
            RuntimeToolStep(
                id="step-1",
                tool_name="recording_echo",
                arguments={"ref": "evidence:step-1"},
                required_evidence_refs=("evidence:step-1",),
            ),
            RuntimeToolStep(
                id="step-2",
                tool_name="retry_stage",
                arguments={"ref": "evidence:step-2"},
                depends_on=("step-1",),
                required_evidence_refs=("evidence:step-2",),
            ),
            RuntimeToolStep(
                id="step-3",
                tool_name="recording_echo",
                arguments={"ref": "evidence:step-3"},
                depends_on=("step-2",),
                required_evidence_refs=("evidence:step-3",),
            ),
        )

    async def test_retry_continues_remaining_frontier_without_repeating_passed_step(
        self,
    ) -> None:
        store = InMemoryTaskStore()
        echo = RecordingEchoTool()
        retry = ConfiguredRetryTool(failures=1)
        tools = ToolRegistry()
        tools.register(echo)
        tools.register(retry)
        runtime = HarnessRuntime(store=store, tools=tools)

        first = await runtime.run(
            contract=self._contract(),
            run_id="run-1",
            steps=self._three_steps(),
        )
        resumed = await runtime.resume_from_checkpoint(run_id="run-1")

        self.assertEqual(first.run.status, TaskRunStatus.RECOVERING)
        self.assertTrue(resumed.accepted)
        self.assertEqual(echo.refs, ["evidence:step-1", "evidence:step-3"])
        self.assertEqual(retry.calls, 2)
        tool_events = [
            event
            for event in resumed.events
            if event.type is TaskEventType.TOOL_RESULT_RECORDED
        ]
        self.assertEqual(
            [event.payload["step_id"] for event in tool_events],
            ["step-1", "step-2", "step-2", "step-3"],
        )
        self.assertIn("continuation", tool_events[-1].payload)
        self.assertEqual(
            tool_events[-1].payload["continuation"]["completed_step_ids"],
            ["step-1", "step-2"],
        )

    async def test_sqlite_restart_restores_plan_frontier_and_acceptance_facts(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "runtime.db"
            first_store = SqliteTaskStore(path)
            first_echo = RecordingEchoTool()
            first_retry = ConfiguredRetryTool(failures=10)
            first_tools = ToolRegistry()
            first_tools.register(first_echo)
            first_tools.register(first_retry)
            first_runtime = HarnessRuntime(store=first_store, tools=first_tools)

            first = await first_runtime.run(
                contract=self._contract(include_test=True),
                run_id="run-restart",
                steps=self._three_steps(),
                acceptance_facts=RuntimeAcceptanceFacts(
                    passed_tests=("trusted-tests",),
                ),
                plan_metadata={"planner": "restart-test"},
            )
            self.assertEqual(first.run.status, TaskRunStatus.RECOVERING)
            first_store.close()

            reopened = SqliteTaskStore(path)
            resumed_echo = RecordingEchoTool()
            resumed_retry = ConfiguredRetryTool(failures=0)
            resumed_tools = ToolRegistry()
            resumed_tools.register(resumed_echo)
            resumed_tools.register(resumed_retry)
            resumed_runtime = HarnessRuntime(store=reopened, tools=resumed_tools)

            resumed = await resumed_runtime.resume_from_checkpoint(
                run_id="run-restart"
            )
            persisted_run = reopened.get_run("run-restart")
            persisted_plan = reopened.get_plan(persisted_run.plan_id)

            self.assertTrue(resumed.accepted)
            self.assertEqual(resumed_echo.refs, ["evidence:step-3"])
            self.assertEqual(resumed_retry.calls, 1)
            self.assertEqual(
                persisted_plan.metadata["planner"],
                {"planner": "restart-test"},
            )
            self.assertIn(
                TaskEventType.ACCEPTANCE_FACTS_RECORDED,
                [event.type for event in resumed.events],
            )
            reopened.close()

    async def test_alternative_tool_completes_original_step_then_continues(self) -> None:
        store = InMemoryTaskStore()
        echo = RecordingEchoTool()
        tools = ToolRegistry()
        tools.register(echo)
        tools.register(AlternativeRequiredTool())
        runtime = HarnessRuntime(store=store, tools=tools)
        steps = list(self._three_steps())
        steps[1] = RuntimeToolStep(
            id="step-2",
            tool_name="alternative_required",
            depends_on=("step-1",),
            required_evidence_refs=("evidence:step-2",),
        )

        await runtime.run(
            contract=self._contract(),
            run_id="run-alternative",
            steps=steps,
        )
        resumed = await runtime.resume_with_alternative_tool(
            run_id="run-alternative",
            alternative_step=RuntimeToolStep(
                id="untrusted-replacement-id",
                tool_name="recording_echo",
                arguments={"ref": "evidence:step-2"},
            ),
        )

        self.assertTrue(resumed.accepted)
        self.assertEqual(
            echo.refs,
            ["evidence:step-1", "evidence:step-2", "evidence:step-3"],
        )
        passed_step_ids = [
            item.step_id
            for item in resumed.step_verifications
            if item.status is StepStatus.PASSED
        ]
        self.assertEqual(passed_step_ids, ["step-1", "step-2", "step-3"])
        alternative_event = next(
            event
            for event in resumed.events
            if event.type is TaskEventType.TOOL_RESULT_RECORDED
            and "alternative" in event.payload
        )
        self.assertEqual(alternative_event.payload["step_id"], "step-2")
        self.assertEqual(
            alternative_event.payload["alternative"]["provided_step_id"],
            "untrusted-replacement-id",
        )

    async def test_alternative_tool_cannot_weaken_original_step_verification(self) -> None:
        store = InMemoryTaskStore()
        echo = RecordingEchoTool()
        tools = ToolRegistry()
        tools.register(echo)
        tools.register(AlternativeRequiredTool())
        runtime = HarnessRuntime(store=store, tools=tools)

        await runtime.run(
            contract=TaskContract(
                id="contract-boundary",
                user_goal="preserve original verification",
                acceptance_criteria=(
                    AcceptanceCriterion(
                        id="required-evidence",
                        description="required evidence exists",
                        type=CriterionType.TOOL_EVIDENCE,
                        evidence_refs=("evidence:required",),
                    ),
                ),
            ),
            run_id="run-boundary",
            steps=[
                RuntimeToolStep(
                    id="step-1",
                    tool_name="alternative_required",
                    required_evidence_refs=("evidence:required",),
                )
            ],
        )
        resumed = await runtime.resume_with_alternative_tool(
            run_id="run-boundary",
            alternative_step=RuntimeToolStep(
                id="replacement",
                tool_name="recording_echo",
                arguments={"ref": "evidence:wrong"},
                required_evidence_refs=("evidence:wrong",),
            ),
        )

        persisted_run = store.get_run("run-boundary")
        persisted_plan = store.get_plan(persisted_run.plan_id)
        self.assertFalse(resumed.accepted)
        self.assertEqual(resumed.run.status, TaskRunStatus.FAILED)
        self.assertEqual(resumed.failure.failed_step, "step-1")
        self.assertEqual(
            persisted_plan.step_by_id("step-1").required_evidence_refs,
            ("evidence:required",),
        )

    async def test_user_approval_resolves_interaction_and_continues_plan(self) -> None:
        store = InMemoryTaskStore()
        long_task_store = InMemoryLongTaskStore()
        echo = RecordingEchoTool()
        tools = ToolRegistry()
        tools.register(ConfirmMutationTool())
        tools.register(echo)
        runtime = HarnessRuntime(
            store=store,
            tools=tools,
            long_task_store=long_task_store,
        )
        steps = (
            RuntimeToolStep(
                id="step-1",
                tool_name="confirm_mutation",
                required_evidence_refs=("evidence:confirmed",),
            ),
            RuntimeToolStep(
                id="step-3",
                tool_name="recording_echo",
                arguments={"ref": "evidence:step-3"},
                depends_on=("step-1",),
                required_evidence_refs=("evidence:step-3",),
            ),
        )

        first = await runtime.run(
            contract=self._contract(),
            run_id="run-approval",
            steps=steps,
        )
        interaction = long_task_store.list_pending_interactions("run-approval")[0]
        resumed = await runtime.resume_with_user_approval(
            run_id="run-approval",
            feedback="approved by operator",
        )
        resolved = long_task_store.get_pending_interaction(interaction.id)

        self.assertEqual(first.run.status, TaskRunStatus.WAITING_USER)
        self.assertTrue(resumed.accepted)
        self.assertEqual(resolved.status, InteractionStatus.RESOLVED)
        self.assertEqual(resolved.resume_token, interaction.resume_token)
        self.assertEqual(resolved.answer, "approved by operator")
        self.assertEqual(echo.refs, ["evidence:step-3"])

    async def test_acceptance_only_approval_is_durable_and_completes(self) -> None:
        store = InMemoryTaskStore()
        long_task_store = InMemoryLongTaskStore()
        runtime = HarnessRuntime(
            store=store,
            tools=ToolRegistry(),
            long_task_store=long_task_store,
        )
        contract = TaskContract(
            id="contract-approval",
            user_goal="obtain explicit operator acceptance",
            acceptance_criteria=(
                AcceptanceCriterion(
                    id="operator-approval",
                    description="operator approves completion",
                    type=CriterionType.HUMAN_APPROVAL,
                ),
            ),
        )

        first = await runtime.run(
            contract=contract,
            run_id="run-acceptance-approval",
            steps=[],
        )
        interaction = long_task_store.list_pending_interactions(
            "run-acceptance-approval",
            status=InteractionStatus.OPEN,
        )[0]
        resumed = await runtime.resume_with_user_approval(
            run_id="run-acceptance-approval",
            feedback="accept",
        )

        self.assertEqual(first.run.status, TaskRunStatus.WAITING_USER)
        self.assertTrue(resumed.accepted)
        self.assertEqual(
            long_task_store.get_pending_interaction(interaction.id).status,
            InteractionStatus.RESOLVED,
        )
        fact_events = [
            event
            for event in resumed.events
            if event.type is TaskEventType.ACCEPTANCE_FACTS_RECORDED
        ]
        self.assertEqual(
            fact_events[-1].payload["human_approvals"],
            ["operator-approval"],
        )


if __name__ == "__main__":
    unittest.main()
