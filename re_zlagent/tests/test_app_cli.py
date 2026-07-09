from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from re_zlagent.app.cli import run_cli  # noqa: E402
from re_zlagent.harness.storage import SqliteTaskStore  # noqa: E402
from re_zlagent.harness.tasking import (  # noqa: E402
    AcceptanceCriterion,
    CriterionType,
    TaskContract,
    TaskEventType,
    TaskRun,
    TaskRunStatus,
)


class AppCliTests(unittest.TestCase):
    def _contract(self) -> TaskContract:
        return TaskContract(
            id="contract-1",
            user_goal="cli controls run",
            acceptance_criteria=(
                AcceptanceCriterion(
                    id="manual",
                    description="manual cli test",
                    type=CriterionType.HUMAN_APPROVAL,
                ),
            ),
        )

    def _sqlite_with_run(
        self,
        tmp: str,
        *,
        run_id: str = "run-1",
        status: TaskRunStatus = TaskRunStatus.RUNNING,
    ) -> Path:
        db_path = Path(tmp) / "tasks.sqlite"
        store = SqliteTaskStore(db_path)
        store.save_contract(self._contract())
        store.create_run(TaskRun(id=run_id, contract_id="contract-1"))
        store.append_event(
            run_id=run_id,
            type=TaskEventType.RUN_CREATED,
            payload={"contract_id": "contract-1"},
        )
        if status is not TaskRunStatus.CREATED:
            store.update_run(store.get_run(run_id).with_status(status))
        store.close()
        return db_path

    def _run_json(self, argv: list[str]) -> tuple[int, dict]:
        stdout = io.StringIO()
        code = run_cli(argv, stdout=stdout)
        return code, json.loads(stdout.getvalue())

    def test_status_reads_sqlite_progress_as_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._sqlite_with_run(tmp)

            code, data = self._run_json([
                "--sqlite",
                str(db_path),
                "status",
                "run-1",
            ])

        self.assertEqual(code, 0)
        self.assertTrue(data["ok"])
        self.assertEqual(data["command"], "status")
        self.assertEqual(data["progress"]["run_id"], "run-1")
        self.assertEqual(data["progress"]["run_status"], "running")

    def test_pause_and_resume_persist_control_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._sqlite_with_run(tmp)

            pause_code, pause_data = self._run_json([
                "--sqlite",
                str(db_path),
                "pause",
                "run-1",
                "--reason",
                "inspect",
                "--actor",
                "cli-test",
            ])
            resume_code, resume_data = self._run_json([
                "--sqlite",
                str(db_path),
                "resume",
                "run-1",
                "continue from current state",
                "--actor",
                "cli-test",
            ])
            store = SqliteTaskStore(db_path)
            events = store.list_events("run-1")
            run = store.get_run("run-1")
            store.close()

        self.assertEqual(pause_code, 0)
        self.assertEqual(pause_data["result"]["run"]["status"], "paused")
        self.assertEqual(pause_data["result"]["event"]["type"], "run_paused")
        self.assertEqual(resume_code, 0)
        self.assertEqual(resume_data["result"]["run"]["status"], "running")
        self.assertEqual(
            resume_data["result"]["event"]["payload"]["feedback"],
            "continue from current state",
        )
        self.assertEqual(run.status, TaskRunStatus.RUNNING)
        self.assertIn(TaskEventType.RUN_PAUSED, [event.type for event in events])
        self.assertIn(TaskEventType.RUN_RESUMED, [event.type for event in events])

    def test_cancel_then_resume_returns_business_rejection_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._sqlite_with_run(tmp)
            cancel_code, cancel_data = self._run_json([
                "--sqlite",
                str(db_path),
                "cancel",
                "run-1",
                "--reason",
                "wrong direction",
            ])
            resume_code, resume_data = self._run_json([
                "--sqlite",
                str(db_path),
                "resume",
                "run-1",
                "try again",
            ])

        self.assertEqual(cancel_code, 0)
        self.assertTrue(cancel_data["ok"])
        self.assertEqual(cancel_data["result"]["run"]["status"], "cancelled")
        self.assertEqual(resume_code, 2)
        self.assertFalse(resume_data["ok"])
        self.assertFalse(resume_data["result"]["accepted"])
        self.assertEqual(resume_data["result"]["event"]["type"], "run_control_rejected")

    def test_fork_creates_new_run_with_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._sqlite_with_run(tmp)

            code, data = self._run_json([
                "--sqlite",
                str(db_path),
                "fork",
                "run-1",
                "run-2",
                "--reason",
                "alternate branch",
            ])
            store = SqliteTaskStore(db_path)
            forked = store.get_run("run-2")
            store.close()

        self.assertEqual(code, 0)
        self.assertTrue(data["ok"])
        self.assertEqual(data["result"]["forked_run"]["id"], "run-2")
        self.assertEqual(
            data["result"]["forked_run"]["metadata"]["forked_from_run_id"],
            "run-1",
        )
        self.assertEqual(forked.metadata["fork_reason"], "alternate branch")

    def test_missing_sqlite_returns_json_argument_error(self) -> None:
        code, data = self._run_json(["status", "run-1"])

        self.assertEqual(code, 2)
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"]["type"], "invalid_arguments")
        self.assertIn("--sqlite", data["error"]["message"])


if __name__ == "__main__":
    unittest.main()
