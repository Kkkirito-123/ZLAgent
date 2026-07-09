from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from harness.progress import ProgressStatus, TaskProgressReader  # noqa: E402
from harness.runtime import HarnessRuntime, RuntimeToolStep  # noqa: E402
from harness.storage import InMemoryTaskStore  # noqa: E402
from harness.tasking import (  # noqa: E402
    AcceptanceCriterion,
    CriterionType,
    StepStatus,
    TaskContract,
    TaskRun,
    TaskRunStatus,
)
from harness.tools import (  # noqa: E402
    Evidence,
    RecommendedNextAction,
    Tool,
    ToolErrorType,
    ToolPermission,
    ToolRegistry,
    ToolResult,
)


class ProgressEvidenceTool(Tool):
    name = "progress_evidence"
    description = "Emit progress evidence."
    permission = ToolPermission.SAFE

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        ref = str(arguments.get("ref") or "progress:evidence")
        return ToolResult.success(
            f"evidence {ref}",
            evidence=[Evidence(type="progress", ref=ref)],
            source=self.name,
        )


class ProgressFailTool(Tool):
    name = "progress_fail"
    description = "Fail recoverably."
    permission = ToolPermission.SAFE

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        return ToolResult.failure(
            "temporary progress failure",
            error_type=ToolErrorType.EXTERNAL_UNAVAILABLE,
            recoverable_by_model=True,
            recommended_next_action=RecommendedNextAction.RETRY,
            source=self.name,
        )


class ProgressReaderTests(unittest.IsolatedAsyncioTestCase):
    def _contract(self, ref: str = "progress:evidence") -> TaskContract:
        return TaskContract(
            id="contract-1",
            user_goal="track progress",
            acceptance_criteria=(
                AcceptanceCriterion(
                    id="has-evidence",
                    description="evidence exists",
                    type=CriterionType.TOOL_EVIDENCE,
                    evidence_refs=(ref,),
                ),
            ),
        )

    def _runtime(self) -> tuple[HarnessRuntime, InMemoryTaskStore]:
        store = InMemoryTaskStore()
        registry = ToolRegistry()
        registry.register(ProgressEvidenceTool())
        registry.register(ProgressFailTool())
        return HarnessRuntime(store=store, tools=registry), store

    async def test_missing_run_returns_missing_snapshot(self) -> None:
        _, store = self._runtime()

        snapshot = TaskProgressReader(store).snapshot("missing")

        self.assertEqual(snapshot.status, ProgressStatus.MISSING)
        self.assertTrue(snapshot.terminal)
        self.assertEqual(snapshot.event_seq, 0)

    async def test_completed_run_snapshot_reports_events_steps_and_acceptance(self) -> None:
        runtime, store = self._runtime()
        await runtime.run(
            contract=self._contract(),
            run_id="run-1",
            steps=(
                RuntimeToolStep(
                    id="step-1",
                    tool_name="progress_evidence",
                    arguments={"ref": "progress:evidence"},
                ),
            ),
        )

        snapshot = TaskProgressReader(store).snapshot("run-1")

        self.assertEqual(snapshot.status, ProgressStatus.COMPLETED)
        self.assertTrue(snapshot.terminal)
        self.assertEqual(snapshot.completed_steps, ("step-1",))
        self.assertEqual(snapshot.step_statuses["step-1"], StepStatus.PASSED)
        self.assertIn("step-1", snapshot.step_checkpoint_ids)
        self.assertTrue(snapshot.accepted)
        self.assertEqual(snapshot.latest_checkpoint_status.value, "completed")

    async def test_recovering_snapshot_reports_failed_step(self) -> None:
        runtime, store = self._runtime()
        await runtime.run(
            contract=self._contract(),
            run_id="run-1",
            steps=(RuntimeToolStep(id="step-1", tool_name="progress_fail"),),
        )

        snapshot = TaskProgressReader(store).snapshot("run-1")

        self.assertEqual(snapshot.status, ProgressStatus.RECOVERING)
        self.assertFalse(snapshot.terminal)
        self.assertEqual(snapshot.failed_step, "step-1")
        self.assertEqual(snapshot.step_statuses["step-1"], StepStatus.FAILED)
        self.assertIsNone(snapshot.accepted)

    async def test_paused_snapshot_is_non_terminal(self) -> None:
        _, store = self._runtime()
        store.save_contract(self._contract())
        store.create_run(
            TaskRun(
                id="run-1",
                contract_id="contract-1",
                status=TaskRunStatus.PAUSED,
            )
        )

        snapshot = TaskProgressReader(store).snapshot("run-1")

        self.assertEqual(snapshot.status, ProgressStatus.PAUSED)
        self.assertFalse(snapshot.terminal)
        self.assertEqual(snapshot.run_status, TaskRunStatus.PAUSED)

    async def test_acceptance_failed_snapshot_reports_failed_criteria(self) -> None:
        runtime, store = self._runtime()
        await runtime.run(
            contract=self._contract(ref="missing:evidence"),
            run_id="run-1",
            steps=(
                RuntimeToolStep(
                    id="step-1",
                    tool_name="progress_evidence",
                    arguments={"ref": "progress:evidence"},
                ),
            ),
        )

        snapshot = TaskProgressReader(store).snapshot("run-1")

        self.assertEqual(snapshot.status, ProgressStatus.ACCEPTANCE_FAILED)
        self.assertTrue(snapshot.terminal)
        self.assertFalse(snapshot.accepted)
        self.assertEqual(snapshot.failed_criteria, ("has-evidence",))
        self.assertEqual(snapshot.step_statuses["step-1"], StepStatus.PASSED)


if __name__ == "__main__":
    unittest.main()
