from __future__ import annotations

import unittest

from backend.harness import HarnessExecution
from backend.harness.accelerate.tool_memo import ToolMemo
from backend.harness.observability.tracer import TraceRecorder
from backend.harness.progress import ProgressEmitter
from backend.tools import Tool, ToolPermission, ToolRegistry, ToolResult


class CountingSearchTool(Tool):
    name = "web_search"
    permission = ToolPermission.SAFE
    is_read_only = True
    is_concurrency_safe = True

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, arguments: dict) -> ToolResult:
        self.calls += 1
        return ToolResult(
            ok=True,
            content="result",
            raw={"sources": [{"url": "https://example.test", "title": "Example"}]},
        )


class MutatingTool(Tool):
    name = "mutate"
    permission = ToolPermission.CONFIRM
    is_read_only = False

    async def execute(self, arguments: dict) -> ToolResult:
        return ToolResult(ok=True, content="written", raw={"path": arguments["path"]})


class HarnessExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_safe_calls_are_memoized_traced_and_redacted(self) -> None:
        registry = ToolRegistry()
        tool = CountingSearchTool()
        registry.register(tool)
        memo = ToolMemo()
        tracer = TraceRecorder()
        progress = ProgressEmitter()
        execution = HarnessExecution(
            registry,
            memo=memo,
            tracer=tracer,
            progress=progress,
        )

        arguments = {"query": "agent", "api_key": "super-secret-value"}
        first = await execution.execute("web_search", arguments, session_id="s1")
        second = await execution.execute("web_search", arguments, session_id="s1")

        self.assertTrue(first.ok and second.ok)
        self.assertEqual(tool.calls, 1)
        self.assertEqual(memo.stats.hits, 1)
        records = tracer.recent_tools(2)
        self.assertFalse(records[0]["cached"])
        self.assertTrue(records[1]["cached"])
        self.assertEqual(records[0]["evidence_count"], 1)
        self.assertNotIn("super-secret-value", records[0]["args_preview"])
        self.assertIn("[REDACTED]", records[0]["args_preview"])
        self.assertEqual(progress.stats.suppressed_no_sink, 2)

    async def test_mutation_requires_confirmation_and_records_side_effect(self) -> None:
        registry = ToolRegistry()
        registry.register(MutatingTool())
        execution = HarnessExecution(registry)

        blocked = await execution.execute("mutate", {"path": "notes/a.md"})
        allowed = await execution.execute(
            "mutate",
            {"path": "notes/a.md"},
            allow_confirm=True,
        )

        self.assertEqual(blocked.status.value, "requires_confirmation")
        self.assertTrue(allowed.ok)
        self.assertEqual(allowed.side_effects[0].target, "path:notes/a.md")

    async def test_unknown_tool_has_recoverable_not_found_error(self) -> None:
        result = await HarnessExecution(ToolRegistry()).execute("missing", {})

        self.assertFalse(result.ok)
        self.assertEqual(result.error_type.value, "not_found")
        self.assertTrue(result.recoverable_by_model)


if __name__ == "__main__":
    unittest.main()
