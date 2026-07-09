from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from re_zlagent.harness.tools import (  # noqa: E402
    SideEffect,
    Tool,
    ToolErrorType,
    ToolPermission,
    ToolRegistry,
    ToolResult,
    ToolResultStatus,
)


class EchoTool(Tool):
    name = "echo"
    description = "Return the provided text."
    permission = ToolPermission.SAFE
    input_schema = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }

    async def execute(self, arguments: dict[str, object]) -> ToolResult:
        return ToolResult.success(str(arguments["text"]), source=self.name)


class ConfirmWriteTool(Tool):
    name = "write_note"
    description = "Write a note."
    permission = ToolPermission.CONFIRM
    is_read_only = False
    side_effects = ("filesystem",)

    async def execute(self, arguments: dict[str, object]) -> ToolResult:
        return ToolResult.success(
            "written",
            side_effects=[
                SideEffect(
                    type="filesystem",
                    target=str(arguments.get("path", "")),
                    risk="medium",
                )
            ],
            source=self.name,
        )


class DeniedTool(Tool):
    name = "danger"
    permission = ToolPermission.DENY


class BrokenTool(Tool):
    name = "broken"
    permission = ToolPermission.SAFE

    async def execute(self, arguments: dict[str, object]) -> ToolResult:
        raise RuntimeError("boom")


class ToolRegistryTests(unittest.IsolatedAsyncioTestCase):
    def test_register_lookup_and_schema_export(self) -> None:
        registry = ToolRegistry()
        registry.register(EchoTool())
        registry.register(ConfirmWriteTool())
        registry.register(DeniedTool())

        self.assertEqual(registry.names(), ["echo", "write_note"])
        self.assertIsInstance(registry.get("echo"), EchoTool)
        self.assertIsNone(registry.get("danger"))

        safe_schemas = registry.to_openai_schema()
        self.assertEqual([item["function"]["name"] for item in safe_schemas], ["echo"])

        all_schemas = registry.to_openai_schema(include_confirm=True)
        self.assertEqual(
            [item["function"]["name"] for item in all_schemas],
            ["echo", "write_note"],
        )

    def test_search_returns_read_only_inventory_by_default(self) -> None:
        registry = ToolRegistry()
        registry.register(EchoTool())
        registry.register(ConfirmWriteTool())

        results = registry.search("return text")

        self.assertEqual(results[0].name, "echo")
        self.assertEqual(results[0].permission, "safe")
        self.assertTrue(results[0].is_read_only)
        self.assertEqual([item.name for item in results], ["echo"])

    def test_search_can_include_confirm_tools(self) -> None:
        registry = ToolRegistry()
        registry.register(EchoTool())
        registry.register(ConfirmWriteTool())

        results = registry.search("write filesystem", include_confirm=True)

        self.assertEqual(results[0].name, "write_note")
        self.assertEqual(results[0].permission, "confirm")
        self.assertFalse(results[0].is_read_only)
        self.assertEqual(results[0].side_effects, ("filesystem",))

    def test_duplicate_tool_name_is_rejected(self) -> None:
        registry = ToolRegistry()
        registry.register(EchoTool())

        with self.assertRaises(ValueError):
            registry.register(EchoTool())

    async def test_execute_safe_tool(self) -> None:
        registry = ToolRegistry()
        registry.register(EchoTool())

        result = await registry.execute("echo", {"text": "hello"})

        self.assertTrue(result.ok)
        self.assertEqual(result.content, "hello")
        self.assertEqual(result.source, "echo")

    async def test_execute_unknown_tool_is_denied(self) -> None:
        registry = ToolRegistry()

        result = await registry.execute("missing", {})

        self.assertFalse(result.ok)
        self.assertEqual(result.status, ToolResultStatus.DENIED)
        self.assertEqual(result.error_type, ToolErrorType.PERMISSION_DENIED)

    async def test_confirm_tool_requires_confirmation_until_trusted(self) -> None:
        registry = ToolRegistry()
        registry.register(ConfirmWriteTool())

        blocked = await registry.execute("write_note", {"path": "/tmp/a.txt"})
        self.assertEqual(blocked.status, ToolResultStatus.REQUIRES_CONFIRMATION)

        allowed = await registry.execute(
            "write_note",
            {"path": "/tmp/a.txt"},
            allow_confirm=True,
        )
        self.assertTrue(allowed.ok)
        self.assertEqual(allowed.side_effects[0].target, "/tmp/a.txt")

    async def test_tool_exception_is_normalized(self) -> None:
        registry = ToolRegistry()
        registry.register(BrokenTool())

        result = await registry.execute("broken", {})

        self.assertFalse(result.ok)
        self.assertEqual(result.error_type, ToolErrorType.TOOL_EXCEPTION)
        self.assertEqual(result.status, ToolResultStatus.ERROR)
        self.assertIn("RuntimeError", result.to_tool_message_content())


if __name__ == "__main__":
    unittest.main()
