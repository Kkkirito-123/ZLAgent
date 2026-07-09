from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from app import AgentApplication  # noqa: E402
from gateway import DeliveryTarget, IncomingMessage  # noqa: E402
from harness.agent import AgentOrchestrator, AgentPlan, StaticAgentPlanner  # noqa: E402
from harness.runtime import HarnessRuntime, RuntimeAcceptanceInput  # noqa: E402
from harness.storage import InMemoryTaskStore  # noqa: E402
from harness.tasking import AcceptanceCriterion, CriterionType, TaskContract, TaskRunStatus  # noqa: E402
from harness.tools import ToolRegistry  # noqa: E402


class AppGatewayTests(unittest.IsolatedAsyncioTestCase):
    def _target(self) -> DeliveryTarget:
        return DeliveryTarget(
            platform="test",
            target_type="user",
            target_id="u1",
            display_name="User",
        )

    def _message(self, text: str = "ship task") -> IncomingMessage:
        return IncomingMessage(
            id="m1",
            platform="test",
            user_id="u1",
            text=text,
            reply_to=self._target(),
            metadata={"channel": "unit"},
        )

    def _contract(self, criterion: AcceptanceCriterion) -> TaskContract:
        return TaskContract(
            id="contract-1",
            user_goal="app handles message",
            acceptance_criteria=(criterion,),
        )

    def _app(self, plan: AgentPlan) -> AgentApplication:
        runtime = HarnessRuntime(
            store=InMemoryTaskStore(),
            tools=ToolRegistry(),
        )
        return AgentApplication(
            orchestrator=AgentOrchestrator(
                planner=StaticAgentPlanner(plan),
                runtime=runtime,
            )
        )

    async def test_handle_message_returns_completed_outgoing_message(self) -> None:
        plan = AgentPlan(
            contract=self._contract(
                AcceptanceCriterion(
                    id="manual-evidence",
                    description="manual evidence",
                    type=CriterionType.TOOL_EVIDENCE,
                    evidence_refs=("manual:evidence",),
                )
            ),
            acceptance=RuntimeAcceptanceInput(evidence_refs=("manual:evidence",)),
        )
        app = self._app(plan)

        result = await app.handle_message(self._message("do it"))

        self.assertTrue(result.agent_result.accepted)
        self.assertEqual(result.outgoing.target.target_id, "u1")
        self.assertEqual(result.outgoing.metadata["status"], "completed")
        self.assertIn("已完成", result.outgoing.text)

    async def test_handle_message_formats_waiting_user(self) -> None:
        plan = AgentPlan(
            contract=self._contract(
                AcceptanceCriterion(
                    id="approval",
                    description="needs approval",
                    type=CriterionType.HUMAN_APPROVAL,
                )
            ),
        )
        app = self._app(plan)

        result = await app.handle_message(self._message())

        self.assertFalse(result.agent_result.accepted)
        self.assertEqual(result.agent_result.runtime_result.run.status, TaskRunStatus.WAITING_USER)
        self.assertEqual(result.outgoing.text, "需要用户确认或补充信息。")

    async def test_handle_message_formats_acceptance_failure(self) -> None:
        plan = AgentPlan(
            contract=self._contract(
                AcceptanceCriterion(
                    id="missing",
                    description="missing evidence",
                    type=CriterionType.TOOL_EVIDENCE,
                    evidence_refs=("missing:evidence",),
                )
            ),
        )
        app = self._app(plan)

        result = await app.handle_message(self._message())

        self.assertEqual(
            result.agent_result.runtime_result.run.status,
            TaskRunStatus.ACCEPTANCE_FAILED,
        )
        self.assertEqual(result.outgoing.text, "任务未通过验收。")

    def test_message_models_validate_required_fields(self) -> None:
        with self.assertRaises(ValueError):
            DeliveryTarget(platform="", target_type="user", target_id="u1")
        with self.assertRaises(ValueError):
            IncomingMessage(
                id="m1",
                platform="test",
                user_id="u1",
                text="",
                reply_to=self._target(),
            )


if __name__ == "__main__":
    unittest.main()
