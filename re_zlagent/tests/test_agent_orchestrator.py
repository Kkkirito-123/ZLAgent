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
from harness.runtime import HarnessRuntime, RuntimeAcceptanceInput, RuntimeToolStep  # noqa: E402
from harness.storage import InMemoryTaskStore  # noqa: E402
from harness.tasking import AcceptanceCriterion, CriterionType, TaskContract, TaskRunStatus  # noqa: E402
from harness.tools import Evidence, Tool, ToolPermission, ToolRegistry, ToolResult  # noqa: E402


class EvidenceTool(Tool):
    name = "evidence"
    description = "Emit evidence for agent orchestrator tests."
    permission = ToolPermission.SAFE

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        ref = str(arguments.get("ref") or "agent:evidence")
        return ToolResult.success(
            f"evidence {ref}",
            evidence=[Evidence(type="agent_test", ref=ref)],
            source=self.name,
        )


class RecordingPlanner:
    def __init__(self, plan: AgentPlan) -> None:
        self.plan_calls: list[AgentRunRequest] = []
        self._plan = plan

    async def plan(self, request: AgentRunRequest) -> AgentPlan:
        self.plan_calls.append(request)
        return self._plan


class AgentOrchestratorTests(unittest.IsolatedAsyncioTestCase):
    def _contract(self, ref: str = "agent:evidence") -> TaskContract:
        return TaskContract(
            id="contract-1",
            user_goal="agent orchestrates runtime",
            acceptance_criteria=(
                AcceptanceCriterion(
                    id="has-evidence",
                    description="agent evidence exists",
                    type=CriterionType.TOOL_EVIDENCE,
                    evidence_refs=(ref,),
                ),
            ),
        )

    def _runtime(self) -> HarnessRuntime:
        registry = ToolRegistry()
        registry.register(EvidenceTool())
        return HarnessRuntime(store=InMemoryTaskStore(), tools=registry)

    async def test_static_plan_runs_through_runtime(self) -> None:
        plan = AgentPlan(
            contract=self._contract(),
            steps=(
                RuntimeToolStep(
                    id="step-1",
                    tool_name="evidence",
                    arguments={"ref": "agent:evidence"},
                ),
            ),
            metadata={"planner": "static"},
        )
        orchestrator = AgentOrchestrator(
            planner=StaticAgentPlanner(plan),
            runtime=self._runtime(),
        )

        result = await orchestrator.run(
            AgentRunRequest(run_id="run-1", user_goal="ship agent layer")
        )

        self.assertTrue(result.accepted)
        self.assertEqual(result.runtime_result.run.status, TaskRunStatus.COMPLETED)
        self.assertEqual(result.planner_metadata["planner"], "static")

    async def test_planner_receives_request_context(self) -> None:
        plan = AgentPlan(
            contract=self._contract(),
            acceptance=RuntimeAcceptanceInput(evidence_refs=("agent:evidence",)),
        )
        planner = RecordingPlanner(plan)
        orchestrator = AgentOrchestrator(planner=planner, runtime=self._runtime())

        await orchestrator.run(
            AgentRunRequest(
                run_id="run-1",
                user_goal="use context",
                context={"channel": "test"},
                model_name="planner-model",
                prompt_version="v1",
            )
        )

        self.assertEqual(len(planner.plan_calls), 1)
        self.assertEqual(planner.plan_calls[0].context["channel"], "test")
        self.assertEqual(planner.plan_calls[0].model_name, "planner-model")

    async def test_plan_acceptance_failure_propagates_runtime_result(self) -> None:
        plan = AgentPlan(
            contract=self._contract(ref="missing:evidence"),
            steps=(
                RuntimeToolStep(
                    id="step-1",
                    tool_name="evidence",
                    arguments={"ref": "agent:evidence"},
                ),
            ),
        )
        orchestrator = AgentOrchestrator(
            planner=StaticAgentPlanner(plan),
            runtime=self._runtime(),
        )

        result = await orchestrator.run(
            AgentRunRequest(run_id="run-1", user_goal="fail acceptance")
        )

        self.assertFalse(result.accepted)
        self.assertEqual(
            result.runtime_result.run.status,
            TaskRunStatus.ACCEPTANCE_FAILED,
        )

    def test_plan_rejects_duplicate_step_ids(self) -> None:
        with self.assertRaises(ValueError):
            AgentPlan(
                contract=self._contract(),
                steps=(
                    RuntimeToolStep(id="step", tool_name="evidence"),
                    RuntimeToolStep(id="step", tool_name="evidence"),
                ),
            )

    def test_request_requires_goal_and_run_id(self) -> None:
        with self.assertRaises(ValueError):
            AgentRunRequest(run_id="", user_goal="goal")
        with self.assertRaises(ValueError):
            AgentRunRequest(run_id="run-1", user_goal="")


if __name__ == "__main__":
    unittest.main()
