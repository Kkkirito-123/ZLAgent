from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from harness.runtime import RunControlAction, RunControlService  # noqa: E402
from harness.storage import InMemoryTaskStore  # noqa: E402
from harness.tasking import (  # noqa: E402
    AcceptanceCriterion,
    CheckpointStatus,
    CriterionType,
    TaskContract,
    TaskEventType,
    TaskRun,
    TaskRunStatus,
)


class RunControlTests(unittest.TestCase):
    def _contract(self) -> TaskContract:
        return TaskContract(
            id="contract-1",
            user_goal="control a run",
            acceptance_criteria=(
                AcceptanceCriterion(
                    id="manual",
                    description="manual control test",
                    type=CriterionType.HUMAN_APPROVAL,
                ),
            ),
        )

    def _store_with_run(
        self,
        *,
        run_id: str = "run-1",
        status: TaskRunStatus = TaskRunStatus.RUNNING,
    ) -> InMemoryTaskStore:
        store = InMemoryTaskStore()
        store.save_contract(self._contract())
        store.create_run(TaskRun(id=run_id, contract_id="contract-1"))
        store.append_event(
            run_id=run_id,
            type=TaskEventType.RUN_CREATED,
            payload={"contract_id": "contract-1"},
        )
        if status is not TaskRunStatus.CREATED:
            store.update_run(store.get_run(run_id).with_status(status))
        return store

    def test_pause_records_event_checkpoint_and_projection(self) -> None:
        store = self._store_with_run()

        result = RunControlService(store).pause(
            "run-1",
            reason="inspect state",
            actor="tester",
        )

        self.assertTrue(result.accepted)
        self.assertEqual(result.action, RunControlAction.PAUSE)
        self.assertEqual(result.run.status, TaskRunStatus.PAUSED)
        self.assertEqual(result.event.type, TaskEventType.RUN_PAUSED)
        self.assertEqual(result.event.payload["previous_status"], "running")
        self.assertEqual(result.checkpoint.status, CheckpointStatus.PAUSED)
        self.assertEqual(store.latest_checkpoint("run-1").id, result.checkpoint.id)
        self.assertEqual(store.get_run("run-1").current_checkpoint_id, result.checkpoint.id)
        self.assertIn(
            TaskEventType.STATUS_CHANGED,
            [event.type for event in store.list_events("run-1")],
        )

    def test_resume_from_pause_records_feedback_and_running_checkpoint(self) -> None:
        store = self._store_with_run()
        control = RunControlService(store)
        control.pause("run-1", reason="needs feedback")

        result = control.resume(
            "run-1",
            feedback="continue from current page",
            actor="tester",
        )

        self.assertTrue(result.accepted)
        self.assertEqual(result.run.status, TaskRunStatus.RUNNING)
        self.assertEqual(result.event.type, TaskEventType.RUN_RESUMED)
        self.assertEqual(result.event.payload["feedback"], "continue from current page")
        self.assertEqual(result.event.payload["previous_status"], "paused")
        self.assertEqual(result.checkpoint.status, CheckpointStatus.RUNNING)
        self.assertEqual(result.metadata["feedback"], "continue from current page")

    def test_cancel_is_terminal_and_resume_is_rejected(self) -> None:
        store = self._store_with_run()
        control = RunControlService(store)

        cancelled = control.cancel("run-1", reason="wrong direction")
        resumed = control.resume("run-1", feedback="try again")

        self.assertTrue(cancelled.accepted)
        self.assertEqual(cancelled.run.status, TaskRunStatus.CANCELLED)
        self.assertEqual(cancelled.event.type, TaskEventType.RUN_CANCELLED)
        self.assertEqual(cancelled.checkpoint.status, CheckpointStatus.CANCELLED)
        self.assertFalse(resumed.accepted)
        self.assertEqual(resumed.action, RunControlAction.RESUME)
        self.assertEqual(resumed.run.status, TaskRunStatus.CANCELLED)
        self.assertEqual(resumed.event.type, TaskEventType.RUN_CONTROL_REJECTED)
        self.assertIn("cancelled", resumed.reason)

    def test_fork_creates_new_run_and_records_source_lineage(self) -> None:
        store = self._store_with_run()
        control = RunControlService(store)
        paused = control.pause("run-1", reason="branch from here")

        result = control.fork(
            "run-1",
            new_run_id="run-2",
            reason="try another approach",
            checkpoint_id=paused.checkpoint.id,
            actor="tester",
        )

        self.assertTrue(result.accepted)
        self.assertEqual(result.action, RunControlAction.FORK)
        self.assertEqual(result.run.id, "run-1")
        self.assertEqual(result.forked_run.id, "run-2")
        self.assertEqual(result.forked_run.status, TaskRunStatus.CREATED)
        self.assertEqual(
            result.forked_run.metadata["forked_from_checkpoint_id"],
            paused.checkpoint.id,
        )
        self.assertEqual(result.event.type, TaskEventType.RUN_FORKED)
        self.assertEqual(result.event.payload["new_run_id"], "run-2")
        fork_events = store.list_events("run-2")
        self.assertEqual(fork_events[0].type, TaskEventType.RUN_CREATED)
        self.assertEqual(fork_events[0].payload["forked_from_run_id"], "run-1")

    def test_fork_rejects_checkpoint_from_another_run(self) -> None:
        store = self._store_with_run()
        store.create_run(TaskRun(id="other-run", contract_id="contract-1"))
        other_checkpoint = store.create_checkpoint(
            run_id="other-run",
            status=CheckpointStatus.RUNNING,
            state={"source": "other"},
        )

        result = RunControlService(store).fork(
            "run-1",
            new_run_id="run-2",
            checkpoint_id=other_checkpoint.id,
        )

        self.assertFalse(result.accepted)
        self.assertEqual(result.event.type, TaskEventType.RUN_CONTROL_REJECTED)
        self.assertIn("does not belong", result.reason)

    def test_fork_rejects_existing_new_run_id(self) -> None:
        store = self._store_with_run()
        store.create_run(TaskRun(id="run-2", contract_id="contract-1"))

        result = RunControlService(store).fork("run-1", new_run_id="run-2")

        self.assertFalse(result.accepted)
        self.assertEqual(result.event.type, TaskEventType.RUN_CONTROL_REJECTED)
        self.assertIn("already exists", result.reason)

    def test_unknown_run_rejects_as_calling_error(self) -> None:
        store = self._store_with_run()

        with self.assertRaises(ValueError):
            RunControlService(store).pause("missing")


if __name__ == "__main__":
    unittest.main()
