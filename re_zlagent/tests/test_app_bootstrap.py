from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from app import ApplicationBootstrapConfig, build_application_container  # noqa: E402
from gateway import DeliveryTarget, IncomingMessage  # noqa: E402
from harness.agent import AgentPlan, StaticAgentPlanner  # noqa: E402
from harness.runtime import RuntimeAcceptanceInput  # noqa: E402
from harness.tasking import AcceptanceCriterion, CriterionType, TaskContract  # noqa: E402


class AppBootstrapTests(unittest.IsolatedAsyncioTestCase):
    def _plan(self) -> AgentPlan:
        return AgentPlan(
            contract=TaskContract(
                id="contract-1",
                user_goal="bootstrap app",
                acceptance_criteria=(
                    AcceptanceCriterion(
                        id="manual-evidence",
                        description="manual evidence",
                        type=CriterionType.TOOL_EVIDENCE,
                        evidence_refs=("manual:evidence",),
                    ),
                ),
            ),
            acceptance=RuntimeAcceptanceInput(evidence_refs=("manual:evidence",)),
        )

    def _message(self) -> IncomingMessage:
        target = DeliveryTarget(
            platform="test",
            target_type="user",
            target_id="u1",
        )
        return IncomingMessage(
            id="m1",
            platform="test",
            user_id="u1",
            text="bootstrap task",
            reply_to=target,
        )

    async def test_bootstrap_container_handles_message_and_exposes_facade(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            container = build_application_container(
                planner=StaticAgentPlanner(self._plan()),
                config=ApplicationBootstrapConfig(workspace_dir=Path(tmp)),
            )

            result = await container.app.handle_message(self._message())
            inventory = container.facade.inventory().to_dict()
            runtime = container.facade.runtime().to_dict()
            container.close()

        self.assertTrue(result.agent_result.accepted)
        self.assertEqual(result.outgoing.metadata["status"], "completed")
        self.assertEqual(inventory["counts"]["tools"], 2)
        self.assertTrue(runtime["components"]["tool_registry"])
        self.assertTrue(runtime["components"]["task_store"])
        self.assertTrue(runtime["components"]["progress_reader"])

    async def test_bootstrap_can_use_sqlite_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "tasks.sqlite"
            container = build_application_container(
                planner=StaticAgentPlanner(self._plan()),
                config=ApplicationBootstrapConfig(
                    sqlite_path=sqlite_path,
                    register_file_tools=False,
                ),
            )

            await container.app.handle_message(self._message())
            events = container.store.list_events("msg-m1")
            self.assertTrue(sqlite_path.exists())
            container.close()

        self.assertGreaterEqual(len(events), 1)

    def test_bootstrap_requires_planner_or_model(self) -> None:
        with self.assertRaises(ValueError):
            build_application_container()

    def test_bootstrap_rejects_planner_and_model_together(self) -> None:
        with self.assertRaises(ValueError):
            build_application_container(
                planner=StaticAgentPlanner(self._plan()),
                model=object(),  # type: ignore[arg-type]
            )


if __name__ == "__main__":
    unittest.main()
