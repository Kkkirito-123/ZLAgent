from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from re_zlagent.harness import build_harness_facade  # noqa: E402
from re_zlagent.harness.progress import TaskProgressReader  # noqa: E402
from re_zlagent.harness.skills import FileSystemSkillLoader  # noqa: E402
from re_zlagent.harness.storage import InMemoryTaskStore  # noqa: E402
from re_zlagent.harness.tools import Tool, ToolPermission, ToolRegistry, ToolResult  # noqa: E402


class FacadeReadTool(Tool):
    name = "facade_read"
    description = "Read something for facade tests."
    permission = ToolPermission.SAFE

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        return ToolResult.success("ok", source=self.name)


class FacadeWriteTool(Tool):
    name = "facade_write"
    description = "Write something for facade tests."
    permission = ToolPermission.CONFIRM
    is_read_only = False
    is_destructive = True
    side_effects = ("filesystem",)
    interrupt_behavior = "cancel"

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        return ToolResult.success("ok", source=self.name)


class BrokenSkillLoader:
    def list(self) -> tuple:
        raise RuntimeError("loader unavailable")


class HarnessFacadeTests(unittest.TestCase):
    def test_inventory_lists_tool_boundaries(self) -> None:
        registry = ToolRegistry()
        registry.register(FacadeReadTool())
        registry.register(FacadeWriteTool())
        facade = build_harness_facade(tool_registry=registry)

        data = facade.inventory().to_dict()

        self.assertEqual(data["counts"]["tools"], 2)
        write_tool = next(item for item in data["tools"] if item["name"] == "facade_write")
        self.assertEqual(write_tool["permission"], "confirm")
        self.assertFalse(write_tool["is_read_only"])
        self.assertTrue(write_tool["is_destructive"])
        self.assertEqual(write_tool["side_effects"], ["filesystem"])
        self.assertEqual(write_tool["interrupt_behavior"], "cancel")

    def test_inventory_lists_loaded_skills(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = Path(tmp) / "skills" / "demo"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "\n".join([
                    "---",
                    "id: demo-skill",
                    "name: Demo Skill",
                    "description: Test skill",
                    "version: 1.2.3",
                    "tags: [demo, test]",
                    "triggers:",
                    "- demo",
                    "---",
                    "Do a demo.",
                ]),
                encoding="utf-8",
            )
            loader = FileSystemSkillLoader(Path(tmp) / "skills")
            loader.load()
            facade = build_harness_facade(skill_loader=loader)

            data = facade.inventory().to_dict()

        self.assertEqual(data["counts"]["skills"], 1)
        self.assertEqual(data["skills"][0]["id"], "demo-skill")
        self.assertEqual(data["skills"][0]["format"], "hermes")
        self.assertEqual(data["skills"][0]["tags"], ["demo", "test"])

    def test_runtime_snapshot_reports_component_presence(self) -> None:
        store = InMemoryTaskStore()
        facade = build_harness_facade(
            tool_registry=ToolRegistry(),
            task_store=store,
            progress_reader=TaskProgressReader(store),
        )

        data = facade.runtime().to_dict()

        self.assertTrue(data["components"]["tool_registry"])
        self.assertTrue(data["components"]["task_store"])
        self.assertTrue(data["components"]["progress_reader"])
        self.assertFalse(data["components"]["skill_loader"])

    def test_inventory_errors_are_data_not_crashes(self) -> None:
        facade = build_harness_facade(skill_loader=BrokenSkillLoader())  # type: ignore[arg-type]

        data = facade.inventory().to_dict()

        self.assertEqual(data["counts"]["errors"], 1)
        self.assertIn("skill_loader: RuntimeError", data["errors"][0])


if __name__ == "__main__":
    unittest.main()
