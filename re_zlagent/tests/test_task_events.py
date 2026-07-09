from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from harness.tasking import TaskEventLog, TaskEventType  # noqa: E402
from harness.tools import Evidence, SideEffect  # noqa: E402


class TaskEventLogTests(unittest.TestCase):
    def test_append_creates_monotonic_sequence_per_run(self) -> None:
        log = TaskEventLog()

        first = log.append(run_id="run-1", type=TaskEventType.RUN_CREATED)
        second = log.append(run_id="run-1", type=TaskEventType.STATUS_CHANGED)
        other = log.append(run_id="run-2", type=TaskEventType.RUN_CREATED)

        self.assertEqual(first.seq, 1)
        self.assertEqual(second.seq, 2)
        self.assertEqual(other.seq, 1)
        self.assertEqual(log.last_seq("run-1"), 2)

    def test_idempotency_key_returns_existing_event(self) -> None:
        log = TaskEventLog()

        first = log.append(
            run_id="run-1",
            type=TaskEventType.TOOL_RESULT_RECORDED,
            payload={"tool": "read_file"},
            idempotency_key="tool-call-1",
        )
        retry = log.append(
            run_id="run-1",
            type=TaskEventType.TOOL_RESULT_RECORDED,
            payload={"tool": "read_file", "retry": True},
            idempotency_key="tool-call-1",
        )

        self.assertIs(first, retry)
        self.assertEqual(len(log.list_for_run("run-1")), 1)
        self.assertEqual(retry.payload, {"tool": "read_file"})

    def test_event_serializes_evidence_and_side_effects(self) -> None:
        log = TaskEventLog()

        event = log.append(
            run_id="run-1",
            type=TaskEventType.TOOL_RESULT_RECORDED,
            evidence=[Evidence(type="file", ref="a.txt", summary="read")],
            side_effects=[SideEffect(type="filesystem", target="a.txt")],
        )

        serialized = event.to_dict()
        self.assertEqual(serialized["evidence"][0]["ref"], "a.txt")
        self.assertEqual(serialized["side_effects"][0]["target"], "a.txt")
        self.assertEqual(serialized["type"], "tool_result_recorded")


if __name__ == "__main__":
    unittest.main()
