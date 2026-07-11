from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from re_zlagent.harness.tasking import (  # noqa: E402
    CheckpointStatus,
    CheckpointStore,
    FailureEnvelope,
    FailureType,
    RecoveryAction,
    TaskRunStatus,
)
from re_zlagent.harness.tools import Evidence, SideEffect  # noqa: E402


class CheckpointTests(unittest.TestCase):
    def test_checkpoint_store_creates_sequence_and_latest(self) -> None:
        store = CheckpointStore()

        first = store.create(
            run_id="run-1",
            status=CheckpointStatus.RUNNING,
            state={"step": 1},
        )
        second = store.create(
            run_id="run-1",
            status=CheckpointStatus.WAITING_USER,
            state={"step": 2},
            resume_from_event_id="evt-2",
        )

        self.assertEqual(first.seq, 1)
        self.assertEqual(second.seq, 2)
        self.assertEqual(store.latest("run-1"), second)
        self.assertEqual(store.get(first.id), first)
        self.assertEqual(second.resume_from_event_id, "evt-2")

    def test_checkpoint_serializes_failure_envelope(self) -> None:
        failure = FailureEnvelope(
            status=TaskRunStatus.RECOVERING,
            failed_step="write_file",
            failure_type=FailureType.UNSAFE_WRITE,
            root_cause="fresh read required",
            recoverable=True,
            recommended_action=RecoveryAction.READ_BEFORE_WRITE,
            evidence=[Evidence(type="write_precondition", ref="a.txt")],
            side_effects=[SideEffect(type="filesystem", target="a.txt")],
        )
        store = CheckpointStore()

        checkpoint = store.create(
            run_id="run-1",
            status=CheckpointStatus.FAILED,
            state={"next": "read_file"},
            failure=failure,
        )

        serialized = checkpoint.to_dict()
        self.assertEqual(serialized["failure"]["failure_type"], "unsafe_write")
        self.assertEqual(
            serialized["failure"]["recommended_action"],
            "read_before_write",
        )
        self.assertEqual(serialized["failure"]["evidence"][0]["ref"], "a.txt")

    def test_duplicate_checkpoint_id_is_rejected(self) -> None:
        store = CheckpointStore()
        store.create(
            run_id="run-1",
            status=CheckpointStatus.RUNNING,
            checkpoint_id="chk-fixed",
        )

        with self.assertRaises(ValueError):
            store.create(
                run_id="run-1",
                status=CheckpointStatus.RUNNING,
                checkpoint_id="chk-fixed",
            )


if __name__ == "__main__":
    unittest.main()
