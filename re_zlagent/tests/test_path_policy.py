from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from harness.sandbox import (  # noqa: E402
    PathViolationType,
    WorkspacePathError,
    WorkspacePathPolicy,
)


class WorkspacePathPolicyTests(unittest.TestCase):
    def test_resolves_relative_path_inside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = WorkspacePathPolicy(root)

            resolved = policy.resolve("notes/todo.txt")

            self.assertEqual(resolved.root, root.resolve())
            self.assertEqual(resolved.relative_path, "notes/todo.txt")
            self.assertEqual(resolved.absolute_path, root.resolve() / "notes/todo.txt")

    def test_empty_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            policy = WorkspacePathPolicy(tmp)

            with self.assertRaises(WorkspacePathError) as raised:
                policy.resolve("")

            self.assertEqual(raised.exception.code, PathViolationType.EMPTY_PATH)

    def test_absolute_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            policy = WorkspacePathPolicy(tmp)

            with self.assertRaises(WorkspacePathError) as raised:
                policy.resolve("/etc/passwd")

            self.assertEqual(raised.exception.code, PathViolationType.ABSOLUTE_PATH)

    def test_windows_drive_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            policy = WorkspacePathPolicy(tmp)

            with self.assertRaises(WorkspacePathError) as raised:
                policy.resolve("C:\\Users\\someone\\secret.txt")

            self.assertEqual(raised.exception.code, PathViolationType.ABSOLUTE_PATH)

    def test_parent_reference_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            policy = WorkspacePathPolicy(tmp)

            with self.assertRaises(WorkspacePathError) as raised:
                policy.resolve("../secret.txt")

            self.assertEqual(raised.exception.code, PathViolationType.PARENT_REFERENCE)

    def test_symlink_escape_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "workspace"
            outside = base / "outside"
            root.mkdir()
            outside.mkdir()
            (outside / "secret.txt").write_text("secret", encoding="utf-8")
            link = root / "link"
            try:
                os.symlink(outside, link)
            except OSError as exc:
                self.skipTest(f"symlink unavailable: {exc}")

            policy = WorkspacePathPolicy(root)

            with self.assertRaises(WorkspacePathError) as raised:
                policy.resolve("link/secret.txt")

            self.assertEqual(raised.exception.code, PathViolationType.ESCAPES_WORKSPACE)

    def test_invalid_workspace_root_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing"

            with self.assertRaises(WorkspacePathError) as raised:
                WorkspacePathPolicy(missing)

            self.assertEqual(raised.exception.code, PathViolationType.INVALID_ROOT)


if __name__ == "__main__":
    unittest.main()
