from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from re_zlagent.harness.storage import InMemoryTaskStore  # noqa: E402
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
from re_zlagent.harness.tools import Evidence, SideEffect  # noqa: E402


class InMemoryTaskStoreTests(unittest.TestCase):
    def _contract(self) -> TaskContract:
        return TaskContract(
            id="contract-1",
            user_goal="ship storage boundary",
            acceptance_criteria=(
                AcceptanceCriterion(
                    id="tests-pass",
                    description="tests pass",
                    type=CriterionType.TEST_RESULT,
                ),
            ),
        )

    def _store_with_run(self) -> tuple[InMemoryTaskStore, TaskRun]:
        store = InMemoryTaskStore()
        store.save_contract(self._contract())
        run = store.create_run(TaskRun(id="run-1", contract_id="contract-1"))
        return store, run

    def test_save_and_get_contract_returns_copy(self) -> None:
        store = InMemoryTaskStore()
        contract = self._contract()

        saved = store.save_contract(contract)
        loaded = store.get_contract("contract-1")

        self.assertEqual(saved.id, "contract-1")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.user_goal, "ship storage boundary")
        self.assertIsNot(loaded, contract)

    def test_create_run_requires_existing_contract(self) -> None:
        store = InMemoryTaskStore()

        with self.assertRaises(ValueError):
            store.create_run(TaskRun(id="run-1", contract_id="missing"))

    def test_duplicate_run_id_is_rejected(self) -> None:
        store, _ = self._store_with_run()

        with self.assertRaises(ValueError):
            store.create_run(TaskRun(id="run-1", contract_id="contract-1"))

    def test_append_event_updates_run_projection_sequence(self) -> None:
        store, _ = self._store_with_run()

        first = store.append_event(
            run_id="run-1",
            type=TaskEventType.RUN_CREATED,
            evidence=[Evidence(type="contract", ref="contract-1")],
        )
        second = store.append_event(
            run_id="run-1",
            type=TaskEventType.STATUS_CHANGED,
            payload={"status": "running"},
        )

        run = store.get_run("run-1")
        self.assertEqual(first.seq, 1)
        self.assertEqual(second.seq, 2)
        self.assertEqual(run.event_seq, 2)
        self.assertEqual(store.list_events("run-1")[0].evidence[0].ref, "contract-1")

    def test_idempotency_key_does_not_duplicate_event(self) -> None:
        store, _ = self._store_with_run()

        first = store.append_event(
            run_id="run-1",
            type=TaskEventType.TOOL_RESULT_RECORDED,
            payload={"tool": "read_file"},
            idempotency_key="tool-call-1",
        )
        retry = store.append_event(
            run_id="run-1",
            type=TaskEventType.TOOL_RESULT_RECORDED,
            payload={"tool": "read_file", "retry": True},
            idempotency_key="tool-call-1",
        )

        self.assertEqual(first.id, retry.id)
        self.assertEqual(first.payload, {"tool": "read_file"})
        self.assertEqual(len(store.list_events("run-1")), 1)
        self.assertEqual(store.get_run("run-1").event_seq, 1)

    def test_update_run_projection_preserves_event_history(self) -> None:
        store, run = self._store_with_run()
        store.append_event(run_id="run-1", type=TaskEventType.RUN_CREATED)

        updated = store.update_run(run.with_status(TaskRunStatus.RUNNING))

        self.assertEqual(updated.status, TaskRunStatus.RUNNING)
        self.assertEqual(len(store.list_events("run-1")), 1)
        self.assertEqual(store.list_events("run-1")[0].type, TaskEventType.RUN_CREATED)

    def test_create_checkpoint_updates_current_anchor(self) -> None:
        store, _ = self._store_with_run()
        event = store.append_event(run_id="run-1", type=TaskEventType.STATUS_CHANGED)

        checkpoint = store.create_checkpoint(
            run_id="run-1",
            status=CheckpointStatus.RUNNING,
            state={"step": "tool_call"},
            resume_from_event_id=event.id,
        )

        run = store.get_run("run-1")
        self.assertEqual(checkpoint.seq, 1)
        self.assertEqual(run.current_checkpoint_id, checkpoint.id)
        self.assertEqual(store.latest_checkpoint("run-1").id, checkpoint.id)
        self.assertEqual(
            store.get_checkpoint(checkpoint.id).resume_from_event_id,
            event.id,
        )

    def test_checkpoint_can_store_failure_envelope(self) -> None:
        store, _ = self._store_with_run()
        failure = FailureEnvelope(
            status=TaskRunStatus.RECOVERING,
            failed_step="write_file",
            failure_type=FailureType.UNSAFE_WRITE,
            root_cause="read-before-write missing",
            recoverable=True,
            recommended_action=RecoveryAction.READ_BEFORE_WRITE,
            evidence=[Evidence(type="write_precondition", ref="a.txt")],
            side_effects=[SideEffect(type="filesystem", target="a.txt")],
        )

        checkpoint = store.create_checkpoint(
            run_id="run-1",
            status=CheckpointStatus.FAILED,
            failure=failure,
        )

        loaded = store.get_checkpoint(checkpoint.id)
        self.assertEqual(loaded.failure.failure_type, FailureType.UNSAFE_WRITE)
        self.assertEqual(
            loaded.failure.recommended_action,
            RecoveryAction.READ_BEFORE_WRITE,
        )

    def test_unknown_run_rejects_events_and_checkpoints(self) -> None:
        store = InMemoryTaskStore()

        with self.assertRaises(ValueError):
            store.append_event(run_id="missing", type=TaskEventType.RUN_CREATED)

        with self.assertRaises(ValueError):
            store.create_checkpoint(
                run_id="missing",
                status=CheckpointStatus.RUNNING,
            )


if __name__ == "__main__":
    unittest.main()
