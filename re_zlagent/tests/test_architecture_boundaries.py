from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))


class ArchitectureBoundaryTests(unittest.TestCase):
    def test_harness_does_not_import_app_or_gateway(self) -> None:
        violations: list[str] = []
        for path in (SRC / "harness").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if self._is_forbidden(alias.name):
                            violations.append(f"{path}: import {alias.name}")
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    if self._is_forbidden(module):
                        violations.append(f"{path}: from {module} import ...")

        self.assertEqual(violations, [])

    def test_app_may_import_harness_and_gateway(self) -> None:
        imported: set[str] = set()
        for path in (SRC / "app").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[0])

        self.assertIn("harness", imported)
        self.assertIn("gateway", imported)

    @staticmethod
    def _is_forbidden(module: str) -> bool:
        root = module.split(".")[0]
        return root in {"app", "gateway"}


if __name__ == "__main__":
    unittest.main()
