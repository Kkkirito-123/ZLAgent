from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from re_zlagent.harness.storage import SqliteTaskStore  # noqa: E402
from re_zlagent.harness.tasking import (  # noqa: E402
    AcceptanceCriterion,
    CheckpointStatus,
    CriterionType,
    FailureEnvelope,
    FailureType,
    RecoveryAction,
    TaskContract,
    TaskEventType,
    TaskRun,
    TaskRunStatus,
)
from re_zlagent.harness.tools import Evidence  # noqa: E402


class SqliteTaskStoreTests(unittest.TestCase):
    def _contract(self) -> TaskContract:
        return TaskContract(
            id="contract-1",
            user_goal="persist task",
            acceptance_criteria=(
                AcceptanceCriterion(
                    id="tests",
                    description="tests pass",
                    type=CriterionType.TEST_RESULT,
                ),
            ),
        )

    def test_persists_contract_run_event_and_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tasks.sqlite"
            store = SqliteTaskStore(db_path)
            store.save_contract(self._contract())
            store.create_run(TaskRun(id="run-1", contract_id="contract-1"))
            event = store.append_event(
                run_id="run-1",
                type=TaskEventType.RUN_CREATED,
                evidence=[Evidence(type="contract", ref="contract-1")],
            )
            checkpoint = store.create_checkpoint(
                run_id="run-1",
                status=CheckpointStatus.RUNNING,
                resume_from_event_id=event.id,
            )
            store.close()

            reopened = SqliteTaskStore(db_path)
            self.assertEqual(reopened.get_contract("contract-1").user_goal, "persist task")
            self.assertEqual(reopened.get_run("run-1").event_seq, 1)
            self.assertEqual(reopened.list_events("run-1")[0].evidence[0].ref, "contract-1")
            self.assertEqual(reopened.latest_checkpoint("run-1").id, checkpoint.id)
            reopened.close()

    def test_idempotency_key_returns_existing_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SqliteTaskStore(Path(tmp) / "tasks.sqlite")
            store.save_contract(self._contract())
            store.create_run(TaskRun(id="run-1", contract_id="contract-1"))

            first = store.append_event(
                run_id="run-1",
                type=TaskEventType.TOOL_RESULT_RECORDED,
                payload={"value": 1},
                idempotency_key="tool-1",
            )
            retry = store.append_event(
                run_id="run-1",
                type=TaskEventType.TOOL_RESULT_RECORDED,
                payload={"value": 2},
                idempotency_key="tool-1",
            )

            self.assertEqual(first.id, retry.id)
            self.assertEqual(retry.payload, {"value": 1})
            self.assertEqual(len(store.list_events("run-1")), 1)
            store.close()

    def test_update_run_does_not_decrease_event_seq(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SqliteTaskStore(Path(tmp) / "tasks.sqlite")
            store.save_contract(self._contract())
            store.create_run(TaskRun(id="run-1", contract_id="contract-1"))
            store.append_event(run_id="run-1", type=TaskEventType.RUN_CREATED)

            updated = store.update_run(
                TaskRun(
                    id="run-1",
                    contract_id="contract-1",
                    status=TaskRunStatus.RUNNING,
                    event_seq=0,
                )
            )

            self.assertEqual(updated.event_seq, 1)
            self.assertEqual(store.get_run("run-1").event_seq, 1)
            store.close()

    def test_checkpoint_failure_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SqliteTaskStore(Path(tmp) / "tasks.sqlite")
            store.save_contract(self._contract())
            store.create_run(TaskRun(id="run-1", contract_id="contract-1"))
            failure = FailureEnvelope(
                status=TaskRunStatus.RECOVERING,
                failed_step="step-1",
                failure_type=FailureType.TOOL_ERROR,
                root_cause="failed",
                recoverable=True,
                recommended_action=RecoveryAction.RETRY,
            )

            checkpoint = store.create_checkpoint(
                run_id="run-1",
                status=CheckpointStatus.FAILED,
                failure=failure,
            )

            loaded = store.get_checkpoint(checkpoint.id)
            self.assertEqual(loaded.failure.failed_step, "step-1")
            self.assertEqual(loaded.failure.recommended_action, RecoveryAction.RETRY)
            store.close()

    def test_unknown_run_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SqliteTaskStore(Path(tmp) / "tasks.sqlite")

            with self.assertRaises(ValueError):
                store.append_event(run_id="missing", type=TaskEventType.RUN_CREATED)
            with self.assertRaises(ValueError):
                store.create_checkpoint(
                    run_id="missing",
                    status=CheckpointStatus.RUNNING,
                )
            store.close()


if __name__ == "__main__":
    unittest.main()
