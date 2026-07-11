from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from re_zlagent.harness.runtime import HarnessRuntime, RuntimeToolStep  # noqa: E402
from re_zlagent.harness.storage import InMemoryLongTaskStore, InMemoryTaskStore  # noqa: E402
from re_zlagent.harness.tasking import (  # noqa: E402
    AcceptanceCriterion,
    CriterionType,
    SideEffectStatus,
    TaskContract,
)
from re_zlagent.harness.tools import (  # noqa: E402
    Evidence,
    SideEffect,
    Tool,
    ToolPermission,
    ToolRegistry,
    ToolResult,
)


class SideEffectTool(Tool):
    name = "side_effect_tool"
    description = "Emit evidence and a side effect."
    permission = ToolPermission.CONFIRM
    is_read_only = False
    side_effects = ("message",)
    outbox_required = True
    side_effect_retry_safe = True

    def plan_side_effects(
        self,
        arguments: dict[str, Any],
    ) -> tuple[SideEffect, ...]:
        return (
            SideEffect(
                type="message",
                target="user-1",
                risk="medium",
                metadata={"channel": "test"},
            ),
        )

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        return ToolResult.success(
            "sent",
            evidence=[
                Evidence(
                    type="runtime",
                    ref="side-effect:evidence",
                    summary="side effect evidence",
                )
            ],
            side_effects=[
                SideEffect(
                    type="message",
                    target="user-1",
                    risk="medium",
                    metadata={"channel": "test"},
                )
            ],
            source=self.name,
        )


class RuntimeSideEffectLedgerTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_records_tool_side_effects_when_long_task_store_exists(self) -> None:
        task_store = InMemoryTaskStore()
        long_task_store = InMemoryLongTaskStore()
        registry = ToolRegistry()
        registry.register(SideEffectTool())
        runtime = HarnessRuntime(
            store=task_store,
            tools=registry,
            long_task_store=long_task_store,
        )
        contract = TaskContract(
            id="contract-1",
            user_goal="record side effects",
            acceptance_criteria=(
                AcceptanceCriterion(
                    id="has-evidence",
                    description="evidence exists",
                    type=CriterionType.TOOL_EVIDENCE,
                    evidence_refs=("side-effect:evidence",),
                ),
            ),
        )

        result = await runtime.run(
            contract=contract,
            run_id="run-1",
            steps=(
                RuntimeToolStep(
                    id="send",
                    tool_name="side_effect_tool",
                    allow_confirm=True,
                ),
            ),
        )

        self.assertTrue(result.accepted)
        side_effects = long_task_store.list_side_effects("run-1")
        self.assertEqual(len(side_effects), 1)
        self.assertEqual(
            side_effects[0].idempotency_key,
            "run:run-1:plan:plan_run-1:step:send:side_effect:0",
        )
        self.assertEqual(side_effects[0].status, SideEffectStatus.CONFIRMED)
        self.assertEqual(side_effects[0].attempt_count, 1)
        self.assertEqual(side_effects[0].metadata["risk"], "medium")


if __name__ == "__main__":
    unittest.main()
