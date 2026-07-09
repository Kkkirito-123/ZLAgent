from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
IMPORT_ROOT = ROOT / "src"
PACKAGE_ROOT = IMPORT_ROOT / "re_zlagent"
sys.path.insert(0, str(IMPORT_ROOT))


class ArchitectureBoundaryTests(unittest.TestCase):
    def test_harness_does_not_import_app_or_gateway(self) -> None:
        violations: list[str] = []
        for path in (PACKAGE_ROOT / "harness").rglob("*.py"):
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
        for path in (PACKAGE_ROOT / "app").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(
                        self._logical_layer(alias.name)
                        for alias in node.names
                    )
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(self._logical_layer(node.module))

        self.assertIn("harness", imported)
        self.assertIn("gateway", imported)

    def test_source_imports_use_re_zlagent_namespace(self) -> None:
        violations: list[str] = []
        for path in PACKAGE_ROOT.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if self._is_bare_layer_import(alias.name):
                            violations.append(f"{path}: import {alias.name}")
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    if self._is_bare_layer_import(module):
                        violations.append(f"{path}: from {module} import ...")

        self.assertEqual(violations, [])

    @staticmethod
    def _is_forbidden(module: str) -> bool:
        return ArchitectureBoundaryTests._logical_layer(module) in {"app", "gateway"}

    @staticmethod
    def _logical_layer(module: str) -> str:
        parts = module.split(".")
        if parts[0] == "re_zlagent" and len(parts) > 1:
            return parts[1]
        return parts[0]

    @staticmethod
    def _is_bare_layer_import(module: str) -> bool:
        root = module.split(".")[0]
        return root in {"app", "gateway", "harness"}


if __name__ == "__main__":
    unittest.main()
