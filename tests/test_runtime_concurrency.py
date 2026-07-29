from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from re_zlagent.harness.runtime import HarnessRuntime, RuntimeToolStep  # noqa: E402
from re_zlagent.harness.storage import InMemoryTaskStore  # noqa: E402
from re_zlagent.harness.tasking import (  # noqa: E402
    AcceptanceCriterion,
    CriterionType,
    TaskContract,
    TaskEventType,
)
from re_zlagent.harness.tools import (  # noqa: E402
    Evidence,
    Tool,
    ToolPermission,
    ToolRegistry,
    ToolResult,
)


class ObservedReadTool(Tool):
    name = "observed_read"
    description = "Observe safe concurrent reads."
    permission = ToolPermission.SAFE
    is_read_only = True
    is_concurrency_safe = True
    input_schema = {
        "type": "object",
        "properties": {"ref": {"type": "string"}},
        "required": ["ref"],
    }

    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.02)
        self.active -= 1
        ref = str(arguments["ref"])
        return ToolResult.success(
            ref,
            evidence=[Evidence(type="read", ref=ref)],
            source=self.name,
        )


class LinearReadTool(ObservedReadTool):
    name = "linear_read"
    is_concurrency_safe = False


class RuntimeConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    def _contract(self) -> TaskContract:
        return TaskContract(
            id="contract-concurrency",
            user_goal="read independent sources",
            acceptance_criteria=(
                AcceptanceCriterion(
                    id="both",
                    description="both reads exist",
                    type=CriterionType.TOOL_EVIDENCE,
                    evidence_refs=("read:a", "read:b"),
                ),
            ),
        )

    async def test_independent_safe_reads_overlap_and_are_observable(self) -> None:
        store = InMemoryTaskStore()
        registry = ToolRegistry()
        tool = ObservedReadTool()
        registry.register(tool)
        runtime = HarnessRuntime(
            store=store,
            tools=registry,
            max_parallel_steps=2,
        )

        result = await runtime.run(
            contract=self._contract(),
            run_id="run-parallel",
            steps=(
                RuntimeToolStep(
                    id="read-a",
                    tool_name=tool.name,
                    arguments={"ref": "read:a"},
                ),
                RuntimeToolStep(
                    id="read-b",
                    tool_name=tool.name,
                    arguments={"ref": "read:b"},
                ),
            ),
        )

        self.assertTrue(result.accepted)
        self.assertEqual(tool.max_active, 2)
        tool_events = [
            event
            for event in result.events
            if event.type is TaskEventType.TOOL_RESULT_RECORDED
        ]
        self.assertEqual(len(tool_events), 2)
        self.assertTrue(
            all(
                event.payload["execution_batch"]["parallel"]
                for event in tool_events
            )
        )
        plan = store.get_plan("plan_run-parallel")
        self.assertTrue(
            plan.step_by_id("read-a").metadata["parallel_safe"]
        )

    async def test_unsafe_or_dependent_steps_remain_linear(self) -> None:
        for tool, dependent in (
            (LinearReadTool(), False),
            (ObservedReadTool(), True),
        ):
            with self.subTest(tool=tool.name, dependent=dependent):
                store = InMemoryTaskStore()
                registry = ToolRegistry()
                registry.register(tool)
                runtime = HarnessRuntime(store=store, tools=registry)
                second_dependencies = ("read-a",) if dependent else ()

                result = await runtime.run(
                    contract=self._contract(),
                    run_id=f"run-{tool.name}-{dependent}",
                    steps=(
                        RuntimeToolStep(
                            id="read-a",
                            tool_name=tool.name,
                            arguments={"ref": "read:a"},
                        ),
                        RuntimeToolStep(
                            id="read-b",
                            tool_name=tool.name,
                            arguments={"ref": "read:b"},
                            depends_on=second_dependencies,
                        ),
                    ),
                )

                self.assertTrue(result.accepted)
                self.assertEqual(tool.max_active, 1)


if __name__ == "__main__":
    unittest.main()
