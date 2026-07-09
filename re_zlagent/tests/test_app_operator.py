from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from re_zlagent.app import ApprovalService, OperatorService  # noqa: E402
from re_zlagent.harness.runtime import HarnessRuntime, RuntimeToolStep  # noqa: E402
from re_zlagent.harness.storage import InMemoryTaskStore  # noqa: E402
from re_zlagent.harness.tasking import (  # noqa: E402
    AcceptanceCriterion,
    CriterionType,
    TaskContract,
    TaskEventType,
    TaskRun,
    TaskRunStatus,
    StepStatus,
)
from re_zlagent.harness.tools import (  # noqa: E402
    Evidence,
    Tool,
    ToolPermission,
    ToolRegistry,
    ToolResult,
)


class ConfirmTool(Tool):
    name = "confirm_write"
    description = "Confirm-tier fake mutation."
    permission = ToolPermission.CONFIRM
    is_read_only = False

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        return ToolResult.success(
            "confirmed",
            evidence=[
                Evidence(
                    type="runtime",
                    ref="confirm:evidence",
                    summary="confirm evidence",
                )
            ],
            source=self.name,
        )


class AppOperatorTests(unittest.TestCase):
    def _store_with_run(
        self,
        *,
        run_id: str = "run-1",
        status: TaskRunStatus = TaskRunStatus.RUNNING,
    ) -> InMemoryTaskStore:
        store = InMemoryTaskStore()
        contract = TaskContract(
            id="contract-1",
            user_goal="operator controls run",
            acceptance_criteria=(
                AcceptanceCriterion(
                    id="manual",
                    description="manual operator test",
                    type=CriterionType.HUMAN_APPROVAL,
                ),
            ),
        )
        store.save_contract(contract)
        store.create_run(TaskRun(id=run_id, contract_id=contract.id))
        store.append_event(
            run_id=run_id,
            type=TaskEventType.RUN_CREATED,
            payload={"contract_id": contract.id},
        )
        if status is not TaskRunStatus.CREATED:
            store.update_run(store.get_run(run_id).with_status(status))
        return store

    def test_status_is_read_only_and_reports_missing_run(self) -> None:
        store = self._store_with_run()
        operator = OperatorService(store)
        before = store.get_run("run-1").event_seq

        found = operator.status("run-1")
        missing = operator.status("missing-run")
        after = store.get_run("run-1").event_seq

        self.assertTrue(found.ok)
        self.assertEqual(found.to_dict()["progress"]["status"], "running")
        self.assertFalse(missing.ok)
        self.assertEqual(missing.to_dict()["progress"]["status"], "missing")
        self.assertEqual(before, after)

    def test_pause_returns_serializable_control_result(self) -> None:
        store = self._store_with_run()
        operator = OperatorService(store)

        response = operator.pause("run-1", reason="inspect", actor="tester")
        data = response.to_dict()
        events = store.list_events("run-1")
        checkpoints = store.list_checkpoints("run-1")

        self.assertTrue(response.ok)
        self.assertEqual(data["command"], "pause")
        self.assertEqual(data["result"]["action"], "pause")
        self.assertEqual(data["result"]["run"]["status"], "paused")
        self.assertEqual(data["result"]["checkpoint"]["status"], "paused")
        self.assertIn(TaskEventType.RUN_PAUSED, [event.type for event in events])
        self.assertEqual(len(checkpoints), 1)

    def test_rejected_control_returns_data_without_mutating_status(self) -> None:
        store = self._store_with_run(status=TaskRunStatus.COMPLETED)
        operator = OperatorService(store)

        response = operator.cancel("run-1", reason="too late", actor="tester")
        data = response.to_dict()
        run = store.get_run("run-1")
        events = store.list_events("run-1")

        self.assertFalse(response.ok)
        self.assertFalse(data["result"]["accepted"])
        self.assertEqual(data["result"]["event"]["type"], "run_control_rejected")
        self.assertEqual(run.status, TaskRunStatus.COMPLETED)
        self.assertIn(
            TaskEventType.RUN_CONTROL_REJECTED,
            [event.type for event in events],
        )

    def test_fork_preserves_lineage_metadata(self) -> None:
        store = self._store_with_run()
        operator = OperatorService(store)

        response = operator.fork(
            "run-1",
            new_run_id="run-2",
            reason="alternate path",
            actor="tester",
        )
        data = response.to_dict()
        forked = store.get_run("run-2")

        self.assertTrue(response.ok)
        self.assertEqual(data["result"]["forked_run"]["id"], "run-2")
        self.assertEqual(forked.metadata["forked_from_run_id"], "run-1")
        self.assertEqual(forked.metadata["fork_reason"], "alternate path")


class AppApprovalTests(unittest.IsolatedAsyncioTestCase):
    def _runtime(self) -> tuple[HarnessRuntime, InMemoryTaskStore]:
        store = InMemoryTaskStore()
        registry = ToolRegistry()
        registry.register(ConfirmTool())
        return HarnessRuntime(store=store, tools=registry), store

    async def test_approval_service_resumes_confirm_checkpoint(self) -> None:
        runtime, store = self._runtime()
        contract = TaskContract(
            id="contract-1",
            user_goal="approve confirm tool",
            acceptance_criteria=(
                AcceptanceCriterion(
                    id="confirm-evidence",
                    description="confirm evidence exists",
                    type=CriterionType.TOOL_EVIDENCE,
                    evidence_refs=("confirm:evidence",),
                ),
            ),
        )
        await runtime.run(
            contract=contract,
            run_id="run-1",
            steps=[
                RuntimeToolStep(
                    id="step-1",
                    tool_name="confirm_write",
                    required_evidence_refs=("confirm:evidence",),
                )
            ],
        )
        waiting_checkpoint = store.latest_checkpoint("run-1")

        response = await ApprovalService(runtime).approve_checkpoint(
            "run-1",
            feedback="approved by user",
        )
        data = response.to_dict()

        self.assertTrue(response.ok)
        self.assertEqual(data["checkpoint_id"], waiting_checkpoint.id)
        self.assertNotEqual(
            data["metadata"]["current_checkpoint_id"],
            waiting_checkpoint.id,
        )
        self.assertTrue(data["result"]["accepted"])
        self.assertEqual(data["result"]["run"]["status"], "completed")
        self.assertEqual(
            data["result"]["acceptance_decision"]["status"],
            "passed",
        )
        self.assertEqual(
            data["result"]["step_verifications"][-1]["status"],
            StepStatus.PASSED.value,
        )
        self.assertIn(
            TaskEventType.USER_INPUT_RECORDED.value,
            [event["type"] for event in data["result"]["events"]],
        )
        self.assertEqual(store.get_run("run-1").status, TaskRunStatus.COMPLETED)

    async def test_approval_service_returns_structured_error(self) -> None:
        runtime, _ = self._runtime()

        response = await ApprovalService(runtime).approve_checkpoint("missing-run")
        data = response.to_dict()

        self.assertFalse(response.ok)
        self.assertEqual(data["command"], "approve_checkpoint")
        self.assertEqual(data["error"]["type"], "value_error")
        self.assertIn("unknown run id", data["error"]["message"])


if __name__ == "__main__":
    unittest.main()
