from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from re_zlagent.harness.tools import (  # noqa: E402
    RecommendedNextAction,
    ToolErrorType,
    ToolRegistry,
    ToolResultStatus,
)
from re_zlagent.harness.tools.builtins import ReadFileTool, create_file_tools  # noqa: E402
from re_zlagent.harness.tools.builtins.file_tools import MAX_BYTES  # noqa: E402


class FileToolsTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_text_file_returns_evidence_and_stamps_read_mark(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            file_path = root / "notes" / "todo.txt"
            file_path.parent.mkdir()
            file_path.write_text("hello", encoding="utf-8")
            read_tool, _ = create_file_tools(root)

            result = await read_tool.execute({"path": "notes/todo.txt"})

            self.assertTrue(result.ok)
            self.assertIn("Path: notes/todo.txt", result.content)
            self.assertIn("hello", result.content)
            self.assertEqual(result.source, "read_file")
            self.assertEqual(result.evidence[0].ref, "notes/todo.txt")
            self.assertEqual(result.evidence[0].metadata["bytes"], 5)
            self.assertFalse(result.evidence[0].metadata["truncated"])
            self.assertIsNotNone(
                read_tool.read_before_write_policy.latest_mark(str(file_path.resolve()))
            )

    async def test_read_missing_file_is_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            read_tool = ReadFileTool(tmp)

            result = await read_tool.execute({"path": "missing.txt"})

            self.assertFalse(result.ok)
            self.assertEqual(result.error_type, ToolErrorType.NOT_FOUND)
            self.assertEqual(result.recommended_next_action, RecommendedNextAction.RETRY)

    async def test_read_directory_is_invalid_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "folder").mkdir()
            read_tool = ReadFileTool(root)

            result = await read_tool.execute({"path": "folder"})

            self.assertFalse(result.ok)
            self.assertEqual(result.error_type, ToolErrorType.INVALID_INPUT)
            self.assertIn("not a regular file", result.error or "")

    async def test_read_binary_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "blob.bin").write_bytes(b"abc\x00def")
            read_tool = ReadFileTool(root)

            result = await read_tool.execute({"path": "blob.bin"})

            self.assertFalse(result.ok)
            self.assertEqual(result.error_type, ToolErrorType.INVALID_INPUT)
            self.assertEqual(result.recommended_next_action, RecommendedNextAction.STOP)

    async def test_read_large_file_truncates_and_marks_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "large.txt").write_bytes(b"a" * (MAX_BYTES + 5))
            read_tool = ReadFileTool(root)

            result = await read_tool.execute({"path": "large.txt"})

            self.assertTrue(result.ok)
            self.assertTrue(result.raw["truncated"])
            self.assertEqual(result.raw["bytes"], MAX_BYTES + 5)
            self.assertEqual(result.raw["returned_bytes"], MAX_BYTES)
            self.assertIn("[...truncated]", result.content)
            self.assertTrue(result.evidence[0].metadata["truncated"])

    async def test_write_new_file_succeeds_with_side_effect(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, write_tool = create_file_tools(root)

            result = await write_tool.execute({
                "path": "notes/todo.txt",
                "content": "new note",
            })

            self.assertTrue(result.ok)
            self.assertEqual((root / "notes" / "todo.txt").read_text(), "new note")
            self.assertEqual(result.source, "write_file")
            self.assertEqual(result.side_effects[0].type, "filesystem")
            self.assertEqual(result.side_effects[0].target, "notes/todo.txt")
            self.assertEqual(result.side_effects[0].risk, "low")

    async def test_write_existing_file_without_read_mark_is_denied(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "note.txt"
            target.write_text("old", encoding="utf-8")
            _, write_tool = create_file_tools(root)

            result = await write_tool.execute({
                "path": "note.txt",
                "content": "new",
            })

            self.assertFalse(result.ok)
            self.assertEqual(result.error_type, ToolErrorType.UNSAFE_WRITE)
            self.assertEqual(
                result.recommended_next_action,
                RecommendedNextAction.READ_BEFORE_WRITE,
            )
            self.assertEqual(target.read_text(encoding="utf-8"), "old")

    async def test_read_then_write_existing_file_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "note.txt"
            target.write_text("old", encoding="utf-8")
            read_tool, write_tool = create_file_tools(root)

            read_result = await read_tool.execute({"path": "note.txt"})
            write_result = await write_tool.execute({
                "path": "note.txt",
                "content": "new",
            })

            self.assertTrue(read_result.ok)
            self.assertTrue(write_result.ok)
            self.assertEqual(target.read_text(encoding="utf-8"), "new")
            self.assertEqual(write_result.side_effects[0].risk, "medium")

    async def test_external_change_after_read_denies_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "note.txt"
            target.write_text("old", encoding="utf-8")
            read_tool, write_tool = create_file_tools(root)

            await read_tool.execute({"path": "note.txt"})
            target.write_text("changed elsewhere", encoding="utf-8")
            result = await write_tool.execute({
                "path": "note.txt",
                "content": "new",
            })

            self.assertFalse(result.ok)
            self.assertEqual(result.error_type, ToolErrorType.UNSAFE_WRITE)
            self.assertEqual(result.raw["required_action"], "read_before_write")
            self.assertEqual(target.read_text(encoding="utf-8"), "changed elsewhere")

    async def test_write_rejects_directory_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "folder").mkdir()
            _, write_tool = create_file_tools(root)

            result = await write_tool.execute({
                "path": "folder",
                "content": "nope",
            })

            self.assertFalse(result.ok)
            self.assertEqual(result.error_type, ToolErrorType.UNSAFE_WRITE)
            self.assertEqual(result.recommended_next_action, RecommendedNextAction.STOP)

    async def test_write_rejects_too_large_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, write_tool = create_file_tools(tmp)

            result = await write_tool.execute({
                "path": "large.txt",
                "content": "a" * (MAX_BYTES + 1),
            })

            self.assertFalse(result.ok)
            self.assertEqual(result.error_type, ToolErrorType.INVALID_INPUT)
            self.assertEqual(result.recommended_next_action, RecommendedNextAction.RETRY)

    async def test_write_symlink_escape_is_unsafe_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "workspace"
            outside = base / "outside"
            root.mkdir()
            outside.mkdir()
            link = root / "link"
            try:
                os.symlink(outside, link)
            except OSError as exc:
                self.skipTest(f"symlink unavailable: {exc}")
            _, write_tool = create_file_tools(root)

            result = await write_tool.execute({
                "path": "link/secret.txt",
                "content": "secret",
            })

            self.assertFalse(result.ok)
            self.assertEqual(result.error_type, ToolErrorType.UNSAFE_WRITE)
            self.assertFalse((outside / "secret.txt").exists())

    async def test_registry_hides_write_tool_until_confirm_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            read_tool, write_tool = create_file_tools(root)
            registry = ToolRegistry()
            registry.register(read_tool)
            registry.register(write_tool)

            schema_names = [item["function"]["name"] for item in registry.to_openai_schema()]
            self.assertEqual(schema_names, ["read_file"])

            blocked = await registry.execute(
                "write_file",
                {"path": "new.txt", "content": "hello"},
            )
            self.assertEqual(blocked.status, ToolResultStatus.REQUIRES_CONFIRMATION)

            allowed = await registry.execute(
                "write_file",
                {"path": "new.txt", "content": "hello"},
                allow_confirm=True,
            )
            self.assertTrue(allowed.ok)
            self.assertEqual((root / "new.txt").read_text(encoding="utf-8"), "hello")


if __name__ == "__main__":
    unittest.main()
