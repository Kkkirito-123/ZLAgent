from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from re_zlagent.harness.runtime import (  # noqa: E402
    DurableWorker,
    HarnessRuntime,
    RunControlService,
    RuntimeToolStep,
    WorkerTickStatus,
)
from re_zlagent.harness.storage import (  # noqa: E402
    InMemoryLongTaskStore,
    InMemoryTaskStore,
    SqliteTaskStore,
    TaskStore,
)
from re_zlagent.harness.tasking import (  # noqa: E402
    AcceptanceCriterion,
    CriterionType,
    PlanDAG,
    ProgramPlan,
    RunLeaseState,
    TaskContract,
    TaskEventType,
    TaskRun,
    TaskRunStatus,
)
from re_zlagent.harness.tools import (  # noqa: E402
    Evidence,
    RecommendedNextAction,
    Tool,
    ToolErrorType,
    ToolPermission,
    ToolRegistry,
    ToolResult,
)


class MutableClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


class EvidenceTool(Tool):
    name = "worker_evidence"
    description = "Return worker completion evidence."
    permission = ToolPermission.SAFE

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        self.calls += 1
        return ToolResult.success(
            "done",
            evidence=[Evidence(type="worker", ref="evidence:worker")],
            source=self.name,
        )


class AlwaysRetryTool(Tool):
    name = "always_retry"
    description = "Always return a transient retryable failure."
    permission = ToolPermission.SAFE

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        return ToolResult.failure(
            "temporary outage",
            error_type=ToolErrorType.EXTERNAL_UNAVAILABLE,
            recoverable_by_model=True,
            recommended_next_action=RecommendedNextAction.RETRY,
            source=self.name,
        )


class ConfirmWorkerTool(Tool):
    name = "confirm_worker"
    description = "Require operator confirmation."
    permission = ToolPermission.CONFIRM
    is_read_only = False

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        return ToolResult.success("confirmed", source=self.name)


class CancelDuringExecuteTool(Tool):
    name = "cancel_during_execute"
    description = "Cancel the current run during a cooperative worker step."
    permission = ToolPermission.SAFE

    def __init__(self, controls: RunControlService, run_id: str) -> None:
        self._controls = controls
        self._run_id = run_id

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        self._controls.cancel(self._run_id, reason="operator cancellation")
        return ToolResult.success(
            "cancel observed",
            evidence=[Evidence(type="worker", ref="evidence:cancel-step")],
            source=self.name,
        )


class DurableWorkerTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _contract(
        *,
        contract_id: str = "contract-1",
        evidence_ref: str = "evidence:worker",
    ) -> TaskContract:
        return TaskContract(
            id=contract_id,
            user_goal="execute through a durable worker",
            acceptance_criteria=(
                AcceptanceCriterion(
                    id="worker-evidence",
                    description="worker evidence exists",
                    type=CriterionType.TOOL_EVIDENCE,
                    evidence_refs=(evidence_ref,),
                ),
            ),
        )

    @staticmethod
    def _create_run(
        store: TaskStore,
        *,
        contract: TaskContract,
        run_id: str,
        steps: tuple[RuntimeToolStep, ...],
    ) -> None:
        plan = ProgramPlan(
            id=f"plan_{run_id}",
            contract_id=contract.id,
            dag=PlanDAG(tuple(step.to_plan_step() for step in steps)),
        )
        store.save_contract(contract)
        store.save_plan(plan)
        store.create_run(
            TaskRun(
                id=run_id,
                contract_id=contract.id,
                plan_id=plan.id,
            )
        )

    async def test_worker_executes_created_run_and_releases_ownership(self) -> None:
        store = InMemoryTaskStore()
        evidence_tool = EvidenceTool()
        tools = ToolRegistry()
        tools.register(evidence_tool)
        runtime = HarnessRuntime(store=store, tools=tools)
        contract = self._contract()
        self._create_run(
            store,
            contract=contract,
            run_id="run-1",
            steps=(
                RuntimeToolStep(
                    id="step-1",
                    tool_name="worker_evidence",
                    required_evidence_refs=("evidence:worker",),
                ),
            ),
        )
        worker = DurableWorker(
            worker_id="worker-1",
            store=store,
            runtime=runtime,
        )

        tick = await worker.run_once()

        self.assertEqual(tick.status, WorkerTickStatus.COMPLETED)
        self.assertTrue(tick.runtime_result.accepted)
        self.assertEqual(evidence_tool.calls, 1)
        self.assertEqual(store.get_run("run-1").status, TaskRunStatus.COMPLETED)
        self.assertEqual(
            store.get_run_lease("run-1").state,
            RunLeaseState.RELEASED,
        )
        event_types = [event.type for event in store.list_events("run-1")]
        self.assertIn(TaskEventType.RUN_CLAIMED, event_types)
        self.assertIn(TaskEventType.RUN_LEASE_RELEASED, event_types)

    async def test_retries_back_off_and_stop_at_budget(self) -> None:
        store = InMemoryTaskStore()
        tools = ToolRegistry()
        tools.register(AlwaysRetryTool())
        runtime = HarnessRuntime(store=store, tools=tools)
        await runtime.run(
            contract=self._contract(evidence_ref="evidence:never"),
            run_id="run-1",
            steps=[RuntimeToolStep(id="step-1", tool_name="always_retry")],
        )
        clock = MutableClock()
        worker = DurableWorker(
            worker_id="worker-1",
            store=store,
            runtime=runtime,
            retry_budget=2,
            base_backoff_seconds=5,
            clock=clock,
        )

        first = await worker.run_once("run-1")
        too_early = await worker.run_once("run-1")
        clock.advance(5)
        second = await worker.run_once("run-1")

        self.assertEqual(first.status, WorkerTickStatus.RETRY_SCHEDULED)
        self.assertEqual(too_early.status, WorkerTickStatus.NO_CANDIDATE)
        self.assertEqual(second.status, WorkerTickStatus.DEAD_LETTER)
        lease = store.get_run_lease("run-1")
        self.assertEqual(lease.state, RunLeaseState.DEAD_LETTER)
        self.assertEqual(lease.attempt_count, 2)
        self.assertIn(
            TaskEventType.RUN_DEAD_LETTERED,
            [event.type for event in store.list_events("run-1")],
        )

    async def test_worker_resumes_persisted_created_run_after_sqlite_reopen(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "worker.sqlite"
            initial = SqliteTaskStore(path)
            contract = self._contract()
            self._create_run(
                initial,
                contract=contract,
                run_id="run-1",
                steps=(
                    RuntimeToolStep(
                        id="step-1",
                        tool_name="worker_evidence",
                        required_evidence_refs=("evidence:worker",),
                    ),
                ),
            )
            initial.close()

            reopened = SqliteTaskStore(path)
            evidence_tool = EvidenceTool()
            tools = ToolRegistry()
            tools.register(evidence_tool)
            worker = DurableWorker(
                worker_id="worker-1",
                store=reopened,
                runtime=HarnessRuntime(store=reopened, tools=tools),
            )

            tick = await worker.run_once("run-1")
            reopened.close()

            verified = SqliteTaskStore(path)
            self.assertEqual(tick.status, WorkerTickStatus.COMPLETED)
            self.assertEqual(
                verified.get_run("run-1").status,
                TaskRunStatus.COMPLETED,
            )
            self.assertEqual(
                verified.get_run_lease("run-1").state,
                RunLeaseState.RELEASED,
            )
            self.assertEqual(evidence_tool.calls, 1)
            verified.close()

    async def test_waiting_user_releases_lease_and_parks_worker(self) -> None:
        store = InMemoryTaskStore()
        long_task_store = InMemoryLongTaskStore()
        tools = ToolRegistry()
        tools.register(ConfirmWorkerTool())
        runtime = HarnessRuntime(
            store=store,
            tools=tools,
            long_task_store=long_task_store,
        )
        contract = self._contract(evidence_ref="evidence:approval")
        self._create_run(
            store,
            contract=contract,
            run_id="run-1",
            steps=(RuntimeToolStep(id="step-1", tool_name="confirm_worker"),),
        )
        worker = DurableWorker(
            worker_id="worker-1",
            store=store,
            runtime=runtime,
        )

        tick = await worker.run_once("run-1")

        self.assertEqual(tick.status, WorkerTickStatus.PARKED)
        self.assertEqual(store.get_run("run-1").status, TaskRunStatus.WAITING_USER)
        self.assertEqual(
            store.get_run_lease("run-1").state,
            RunLeaseState.RELEASED,
        )
        self.assertEqual(
            len(long_task_store.list_pending_interactions("run-1")),
            1,
        )

    async def test_operator_cancel_stops_before_the_next_step(self) -> None:
        store = InMemoryTaskStore()
        controls = RunControlService(store)
        evidence_tool = EvidenceTool()
        tools = ToolRegistry()
        tools.register(CancelDuringExecuteTool(controls, "run-1"))
        tools.register(evidence_tool)
        runtime = HarnessRuntime(store=store, tools=tools)
        contract = self._contract(evidence_ref="evidence:worker")
        self._create_run(
            store,
            contract=contract,
            run_id="run-1",
            steps=(
                RuntimeToolStep(
                    id="step-1",
                    tool_name="cancel_during_execute",
                    required_evidence_refs=("evidence:cancel-step",),
                ),
                RuntimeToolStep(
                    id="step-2",
                    tool_name="worker_evidence",
                    depends_on=("step-1",),
                ),
            ),
        )
        worker = DurableWorker(
            worker_id="worker-1",
            store=store,
            runtime=runtime,
        )

        tick = await worker.run_once("run-1")

        self.assertEqual(tick.status, WorkerTickStatus.PARKED)
        self.assertEqual(store.get_run("run-1").status, TaskRunStatus.CANCELLED)
        self.assertEqual(evidence_tool.calls, 0)
        self.assertEqual(store.latest_checkpoint("run-1").status.value, "cancelled")
        self.assertNotIn(
            TaskEventType.RUN_COMPLETED,
            [event.type for event in store.list_events("run-1")],
        )


if __name__ == "__main__":
    unittest.main()
