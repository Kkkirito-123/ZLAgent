from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"


class ArchitectureBoundaryTests(unittest.TestCase):
    def test_harness_does_not_monkey_patch_execute(self) -> None:
        offenders: list[str] = []
        for path in (BACKEND / "harness").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                targets = []
                if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                    targets = (
                        list(node.targets)
                        if isinstance(node, ast.Assign)
                        else [node.target]
                    )
                if any(
                    isinstance(target, ast.Attribute) and target.attr == "execute"
                    for target in targets
                ):
                    offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(offenders, [])

    def test_tool_invocation_is_centralized(self) -> None:
        forbidden = (
            "await tool.execute(",
            "await skill_tool.execute(",
            "await self._registry.execute(",
            "await tool_registry.execute(",
        )
        offenders: list[str] = []
        allowed = {
            Path("backend/tools/registry.py"),
            Path("backend/harness/execution.py"),
        }
        for path in BACKEND.rglob("*.py"):
            rel = path.relative_to(ROOT)
            if rel in allowed:
                continue
            source = path.read_text(encoding="utf-8")
            if any(token in source for token in forbidden):
                offenders.append(str(rel))
        self.assertEqual(offenders, [])

    def test_known_operator_surfaces_use_harness_execution(self) -> None:
        paths = (
            "backend/agent/loop.py",
            "backend/agent/tool_loop/runner.py",
            "backend/agent/confirmation/flow.py",
            "backend/api/tools.py",
            "backend/harness/extensions/lifecycle.py",
        )
        for relative in paths:
            with self.subTest(path=relative):
                source = (ROOT / relative).read_text(encoding="utf-8")
                self.assertIn("execution", source.lower())
                self.assertIn(".execute(", source)

    def test_repository_instructions_have_one_source_of_truth(self) -> None:
        self.assertTrue((ROOT / "AGENTS.md").is_file())
        self.assertTrue((ROOT / "AGENTS.zh-CN.md").is_file())
        self.assertEqual(
            (ROOT / "CLAUDE.md").read_text(encoding="utf-8"),
            "# Claude Code Instructions\n\n@AGENTS.md\n",
        )


if __name__ == "__main__":
    unittest.main()
