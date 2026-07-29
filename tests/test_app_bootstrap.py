from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from re_zlagent.app import ApplicationBootstrapConfig, build_application_container  # noqa: E402
from re_zlagent.gateway import DeliveryTarget, IncomingMessage  # noqa: E402
from re_zlagent.harness.agent import (  # noqa: E402
    AgentPlan,
    AgentRunRequest,
    StaticAgentPlanner,
)
from re_zlagent.harness.runtime import RuntimeToolStep  # noqa: E402
from re_zlagent.harness.tasking import AcceptanceCriterion, CriterionType, TaskContract  # noqa: E402
from re_zlagent.harness.tools import (  # noqa: E402
    Evidence,
    Tool,
    ToolPermission,
    ToolRegistry,
    ToolResult,
)


class BootstrapEvidenceTool(Tool):
    name = "bootstrap_evidence"
    description = "Emit trusted test evidence through the runtime tool path."
    permission = ToolPermission.SAFE

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        return ToolResult.success(
            "bootstrap evidence",
            evidence=[Evidence(type="test", ref="bootstrap:evidence")],
            source=self.name,
        )


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
                        evidence_refs=("bootstrap:evidence",),
                    ),
                ),
            ),
            steps=(
                RuntimeToolStep(id="evidence", tool_name="bootstrap_evidence"),
            ),
        )

    def _registry(self) -> ToolRegistry:
        registry = ToolRegistry()
        registry.register(BootstrapEvidenceTool())
        return registry

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
                tool_registry=self._registry(),
                config=ApplicationBootstrapConfig(workspace_dir=Path(tmp)),
            )

            result = await container.app.handle_message(self._message())
            general_result = await container.general_agent.run(
                AgentRunRequest(
                    run_id="general-agent-bootstrap",
                    user_goal="bootstrap general task",
                )
            )
            inventory = container.facade.inventory().to_dict()
            runtime = container.facade.runtime().to_dict()
            operator_snapshot = container.operator.status("missing-run").to_dict()
            approvals = container.approvals
            long_task_store = container.long_task_store
            container.close()

        self.assertTrue(result.agent_result.accepted)
        self.assertTrue(general_result.verified)
        self.assertEqual(result.outgoing.metadata["status"], "completed")
        self.assertEqual(inventory["counts"]["tools"], 3)
        self.assertTrue(runtime["components"]["tool_registry"])
        self.assertTrue(runtime["components"]["task_store"])
        self.assertTrue(runtime["components"]["progress_reader"])
        self.assertFalse(operator_snapshot["ok"])
        self.assertIsNotNone(approvals)
        self.assertIsNotNone(long_task_store)

    async def test_bootstrap_can_use_sqlite_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "tasks.sqlite"
            container = build_application_container(
                planner=StaticAgentPlanner(self._plan()),
                tool_registry=self._registry(),
                config=ApplicationBootstrapConfig(
                    sqlite_path=sqlite_path,
                    register_file_tools=False,
                ),
            )

            await container.app.handle_message(self._message())
            events = container.store.list_events("msg-m1")
            container.long_task_store.list_side_effects("msg-m1")
            self.assertTrue(sqlite_path.exists())
            container.close()

        self.assertGreaterEqual(len(events), 1)

    async def test_bootstrap_registers_controlled_skill_installation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            imports = root / "imports"
            managed = root / "skills"
            candidate = imports / "demo"
            candidate.mkdir(parents=True)
            managed.mkdir()
            (candidate / "SKILL.md").write_text(
                "---\nid: demo\nname: Demo\n---\nUse evidence.\n",
                encoding="utf-8",
            )
            container = build_application_container(
                planner=StaticAgentPlanner(self._plan()),
                tool_registry=self._registry(),
                config=ApplicationBootstrapConfig(
                    register_file_tools=False,
                    skill_import_dir=imports,
                    skills_dir=managed,
                ),
            )

            before = container.facade.inventory().to_dict()
            result = await container.tools.execute(
                "install_skill",
                {"source_path": "demo", "skill_id": "demo"},
                allow_confirm=True,
            )
            after = container.facade.inventory().to_dict()
            container.close()

        self.assertEqual(before["counts"]["skills"], 0)
        self.assertIn("install_skill", [item["name"] for item in before["tools"]])
        self.assertTrue(result.ok)
        self.assertEqual(after["counts"]["skills"], 1)
        self.assertEqual(after["skills"][0]["id"], "demo")

    def test_bootstrap_requires_both_skill_directories(self) -> None:
        with self.assertRaises(ValueError):
            ApplicationBootstrapConfig(skill_import_dir=Path("imports"))

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
