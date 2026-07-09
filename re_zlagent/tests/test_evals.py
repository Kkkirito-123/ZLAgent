from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from harness.agent import (  # noqa: E402
    AgentOrchestrator,
    AgentPlan,
    AgentRunRequest,
    StaticAgentPlanner,
)
from harness.evals import (  # noqa: E402
    AgentEvalRunner,
    EvalScenario,
    RunHealthMonitor,
    RunHealthStatus,
    evaluate_agent_result,
)
from harness.runtime import HarnessRuntime, RuntimeToolStep  # noqa: E402
from harness.storage import InMemoryTaskStore  # noqa: E402
from harness.tasking import (  # noqa: E402
    AcceptanceCriterion,
    CheckpointStatus,
    CriterionType,
    RecoveryAction,
    TaskContract,
    TaskEventType,
    TaskRunStatus,
)
from harness.tools import (  # noqa: E402
    Evidence,
    RecommendedNextAction,
    Tool,
    ToolErrorType,
    ToolPermission,
    ToolRegistry,
    ToolResult,
)


class EvalEvidenceTool(Tool):
    name = "eval_evidence"
    description = "Emit deterministic eval evidence."
    permission = ToolPermission.SAFE

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        ref = str(arguments.get("ref") or "eval:evidence")
        return ToolResult.success(
            f"evidence {ref}",
            evidence=[Evidence(type="eval", ref=ref)],
            source=self.name,
        )


class EvalFailingTool(Tool):
    name = "eval_fail"
    description = "Fail in a recoverable way."
    permission = ToolPermission.SAFE

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        return ToolResult.failure(
            "temporary eval failure",
            error_type=ToolErrorType.EXTERNAL_UNAVAILABLE,
            recoverable_by_model=True,
            recommended_next_action=RecommendedNextAction.RETRY,
            source=self.name,
        )


class EvalTests(unittest.IsolatedAsyncioTestCase):
    def _contract(self, ref: str = "eval:evidence") -> TaskContract:
        return TaskContract(
            id="contract-1",
            user_goal="evaluate agent run",
            acceptance_criteria=(
                AcceptanceCriterion(
                    id="has-evidence",
                    description="evidence exists",
                    type=CriterionType.TOOL_EVIDENCE,
                    evidence_refs=(ref,),
                ),
            ),
        )

    def _orchestrator(self, plan: AgentPlan) -> AgentOrchestrator:
        registry = ToolRegistry()
        registry.register(EvalEvidenceTool())
        registry.register(EvalFailingTool())
        runtime = HarnessRuntime(store=InMemoryTaskStore(), tools=registry)
        return AgentOrchestrator(
            planner=StaticAgentPlanner(plan),
            runtime=runtime,
        )

    async def test_eval_runner_passes_successful_scenario(self) -> None:
        plan = AgentPlan(
            contract=self._contract(),
            steps=(
                RuntimeToolStep(
                    id="step-1",
                    tool_name="eval_evidence",
                    arguments={"ref": "eval:evidence"},
                ),
            ),
        )
        runner = AgentEvalRunner(self._orchestrator(plan))
        scenario = EvalScenario(
            id="success",
            name="successful evidence run",
            request=AgentRunRequest(run_id="run-1", user_goal="run eval"),
            expected_accepted=True,
            expected_status=TaskRunStatus.COMPLETED,
            required_event_types=(
                TaskEventType.TOOL_RESULT_RECORDED,
                TaskEventType.ACCEPTANCE_EVALUATED,
                TaskEventType.RUN_COMPLETED,
            ),
            required_checkpoint_statuses=(CheckpointStatus.COMPLETED,),
            required_evidence_refs=("eval:evidence",),
            max_tool_failures=0,
        )

        result = await runner.run_scenario(scenario)

        self.assertTrue(result.passed)
        self.assertEqual(result.score, 1.0)
        self.assertEqual(result.run_status, TaskRunStatus.COMPLETED)

    async def test_eval_runner_reports_expectation_failures_without_changing_runtime(self) -> None:
        plan = AgentPlan(
            contract=self._contract(ref="missing:evidence"),
            steps=(
                RuntimeToolStep(
                    id="step-1",
                    tool_name="eval_evidence",
                    arguments={"ref": "eval:evidence"},
                ),
            ),
        )
        orchestrator = self._orchestrator(plan)
        agent_result = await orchestrator.run(
            AgentRunRequest(run_id="run-1", user_goal="run eval")
        )
        scenario = EvalScenario(
            id="expected-pass",
            name="should have passed",
            request=AgentRunRequest(run_id="ignored", user_goal="unused"),
            expected_accepted=True,
            expected_status=TaskRunStatus.COMPLETED,
            required_evidence_refs=("missing:evidence",),
        )

        eval_result = evaluate_agent_result(scenario, agent_result)

        self.assertFalse(eval_result.passed)
        self.assertLess(eval_result.score, 1.0)
        self.assertEqual(agent_result.runtime_result.run.status, TaskRunStatus.ACCEPTANCE_FAILED)
        self.assertIn("expected accepted=True", eval_result.failures[0])

    async def test_eval_suite_aggregates_pass_rate(self) -> None:
        plan = AgentPlan(
            contract=self._contract(),
            steps=(
                RuntimeToolStep(
                    id="step-1",
                    tool_name="eval_evidence",
                    arguments={"ref": "eval:evidence"},
                ),
            ),
        )
        runner = AgentEvalRunner(self._orchestrator(plan))
        suite = await runner.run_suite([
            EvalScenario(
                id="a",
                name="accepted",
                request=AgentRunRequest(run_id="run-a", user_goal="run eval"),
                expected_accepted=True,
            ),
            EvalScenario(
                id="b",
                name="wrong status",
                request=AgentRunRequest(run_id="run-b", user_goal="run eval"),
                expected_status=TaskRunStatus.FAILED,
            ),
        ])

        self.assertEqual(suite.total, 2)
        self.assertEqual(suite.passed_count, 1)
        self.assertEqual(suite.failed_count, 1)
        self.assertEqual(suite.pass_rate, 0.5)

    async def test_health_monitor_marks_recoverable_tool_failure(self) -> None:
        plan = AgentPlan(
            contract=self._contract(),
            steps=(RuntimeToolStep(id="step-1", tool_name="eval_fail"),),
        )
        orchestrator = self._orchestrator(plan)
        agent_result = await orchestrator.run(
            AgentRunRequest(run_id="run-1", user_goal="run eval")
        )

        snapshot = RunHealthMonitor().inspect(agent_result)

        self.assertEqual(snapshot.status, RunHealthStatus.RECOVERABLE)
        self.assertEqual(snapshot.next_action, RecoveryAction.RETRY)
        self.assertEqual(snapshot.tool_failure_count, 1)
        self.assertFalse(snapshot.terminal)

    async def test_health_monitor_marks_blocked_human_acceptance(self) -> None:
        plan = AgentPlan(
            contract=TaskContract(
                id="contract-1",
                user_goal="needs approval",
                acceptance_criteria=(
                    AcceptanceCriterion(
                        id="approval",
                        description="needs human approval",
                        type=CriterionType.HUMAN_APPROVAL,
                    ),
                ),
            )
        )
        orchestrator = self._orchestrator(plan)
        agent_result = await orchestrator.run(
            AgentRunRequest(run_id="run-1", user_goal="run eval")
        )

        snapshot = RunHealthMonitor().inspect(agent_result)

        self.assertEqual(snapshot.status, RunHealthStatus.WAITING_USER)
        self.assertEqual(snapshot.next_action, RecoveryAction.ASK_USER)
        self.assertEqual(snapshot.blocked_criteria, ("approval",))

    def test_scenario_requires_at_least_one_expectation(self) -> None:
        with self.assertRaises(ValueError):
            EvalScenario(
                id="empty",
                name="empty",
                request=AgentRunRequest(run_id="run-1", user_goal="run eval"),
            )


if __name__ == "__main__":
    unittest.main()
