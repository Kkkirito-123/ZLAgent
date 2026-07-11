from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from re_zlagent.harness.progress import LongTaskProgressReader, ProgressStatus  # noqa: E402
from re_zlagent.harness.runtime import ParkedRunKind  # noqa: E402
from re_zlagent.harness.storage import InMemoryLongTaskStore, InMemoryTaskStore  # noqa: E402
from re_zlagent.harness.tasking import (  # noqa: E402
    AcceptanceCriterion,
    CheckpointStatus,
    CriterionType,
    PendingInteraction,
    PlanDAG,
    PlanStep,
    ProgramPlan,
    TaskContract,
    TaskRun,
    TaskRunStatus,
)


class LongTaskProgressReaderTests(unittest.TestCase):
    def _contract(self) -> TaskContract:
        return TaskContract(
            id="contract-1",
            user_goal="observe long task progress",
            acceptance_criteria=(
                AcceptanceCriterion(
                    id="approval",
                    description="operator approves",
                    type=CriterionType.HUMAN_APPROVAL,
                ),
            ),
        )

    def _plan(self) -> ProgramPlan:
        return ProgramPlan(
            id="program-1",
            contract_id="contract-1",
            dag=PlanDAG((
                PlanStep(id="ask", title="Ask", metadata={"requires_user": True}),
                PlanStep(
                    id="verify",
                    title="Verify",
                    depends_on=("ask",),
                    metadata={"read_only": True},
                ),
            )),
        )

    def test_waiting_user_snapshot_combines_progress_projection_and_parked_state(self) -> None:
        store = InMemoryTaskStore()
        store.save_contract(self._contract())
        store.create_run(
            TaskRun(
                id="run-1",
                contract_id="contract-1",
                status=TaskRunStatus.WAITING_USER,
            )
        )
        checkpoint = store.create_checkpoint(
            run_id="run-1",
            status=CheckpointStatus.WAITING_USER,
            checkpoint_id="chk-1",
        )
        long_task_store = InMemoryLongTaskStore()
        long_task_store.save_pending_interaction(
            PendingInteraction(
                id="pi-1",
                run_id="run-1",
                checkpoint_id=checkpoint.id,
                question="Approve resume?",
                required_by_step_id="ask",
                resume_token="resume-token-1",
            )
        )

        snapshot = LongTaskProgressReader(
            store,
            long_task_store=long_task_store,
        ).snapshot(
            "run-1",
            program_plan=self._plan(),
        )

        self.assertEqual(snapshot.task.status, ProgressStatus.WAITING_USER)
        self.assertEqual(snapshot.parked.kind, ParkedRunKind.WAITING_USER)
        self.assertEqual(snapshot.projection.pending_interaction_ids, ("pi-1",))
        self.assertEqual(snapshot.projection.blocked_step_ids, ("ask",))
        self.assertEqual(snapshot.dag_execution.frontier_step_ids, ())
        self.assertEqual(snapshot.metadata["pending_interaction_count"], 1)


if __name__ == "__main__":
    unittest.main()
