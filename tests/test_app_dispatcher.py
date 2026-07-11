from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from re_zlagent.app import ApplicationDispatcher, AgentApplication  # noqa: E402
from re_zlagent.gateway import DeliveryTarget, IncomingMessage, OutgoingMessage  # noqa: E402
from re_zlagent.harness.agent import AgentOrchestrator, AgentPlan, StaticAgentPlanner  # noqa: E402
from re_zlagent.harness.runtime import HarnessRuntime, RuntimeToolStep  # noqa: E402
from re_zlagent.harness.storage import InMemoryTaskStore  # noqa: E402
from re_zlagent.harness.tasking import AcceptanceCriterion, CriterionType, TaskContract  # noqa: E402
from re_zlagent.harness.tools import (  # noqa: E402
    Evidence,
    Tool,
    ToolPermission,
    ToolRegistry,
    ToolResult,
)


class DispatchEvidenceTool(Tool):
    name = "dispatch_evidence"
    description = "Emit trusted dispatcher test evidence through the runtime."
    permission = ToolPermission.SAFE

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        return ToolResult.success(
            "dispatch evidence",
            evidence=[Evidence(type="test", ref="dispatch:evidence")],
            source=self.name,
        )


class RecordingGateway:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[OutgoingMessage] = []

    async def send(self, message: OutgoingMessage) -> None:
        if self.fail:
            raise RuntimeError("send failed")
        self.sent.append(message)


class AppDispatcherTests(unittest.IsolatedAsyncioTestCase):
    def _message(self) -> IncomingMessage:
        target = DeliveryTarget(platform="test", target_type="user", target_id="u1")
        return IncomingMessage(
            id="m1",
            platform="test",
            user_id="u1",
            text="dispatch task",
            reply_to=target,
        )

    def _app(self) -> AgentApplication:
        plan = AgentPlan(
            contract=TaskContract(
                id="contract-1",
                user_goal="dispatch",
                acceptance_criteria=(
                    AcceptanceCriterion(
                        id="manual-evidence",
                        description="manual evidence",
                        type=CriterionType.TOOL_EVIDENCE,
                        evidence_refs=("dispatch:evidence",),
                    ),
                ),
            ),
            steps=(
                RuntimeToolStep(id="evidence", tool_name="dispatch_evidence"),
            ),
        )
        tools = ToolRegistry()
        tools.register(DispatchEvidenceTool())
        runtime = HarnessRuntime(store=InMemoryTaskStore(), tools=tools)
        return AgentApplication(
            orchestrator=AgentOrchestrator(
                planner=StaticAgentPlanner(plan),
                runtime=runtime,
            )
        )

    async def test_dispatch_sends_outgoing_message(self) -> None:
        gateway = RecordingGateway()
        dispatcher = ApplicationDispatcher(app=self._app(), gateway=gateway)

        result = await dispatcher.dispatch(self._message())

        self.assertTrue(result.sent)
        self.assertIsNone(result.error)
        self.assertEqual(len(gateway.sent), 1)
        self.assertEqual(gateway.sent[0].metadata["status"], "completed")

    async def test_dispatch_delivery_failure_is_data(self) -> None:
        gateway = RecordingGateway(fail=True)
        dispatcher = ApplicationDispatcher(app=self._app(), gateway=gateway)

        result = await dispatcher.dispatch(self._message())

        self.assertFalse(result.sent)
        self.assertIn("RuntimeError", result.error)
        self.assertEqual(result.outgoing.metadata["status"], "completed")


if __name__ == "__main__":
    unittest.main()
