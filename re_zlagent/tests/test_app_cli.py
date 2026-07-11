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
from re_zlagent.harness.model import ModelMessage, ModelResponse  # noqa: E402
from re_zlagent.harness.storage import SqliteTaskStore  # noqa: E402
from re_zlagent.harness.tasking import (  # noqa: E402
    AcceptanceCriterion,
    CriterionType,
    TaskContract,
    TaskEventType,
    TaskRun,
    TaskRunStatus,
)


class CliPlanModel:
    def __init__(self, plan: dict) -> None:
        self.plan = plan
        self.messages: tuple[ModelMessage, ...] = ()

    async def complete(self, messages: tuple[ModelMessage, ...]) -> ModelResponse:
        self.messages = messages
        return ModelResponse(
            content=json.dumps(self.plan),
            raw={"provider": "test", "model": "cli-plan-model"},
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

    def _run_json(
        self, argv: list[str], *, model=None, environ=None
    ) -> tuple[int, dict]:
        stdout = io.StringIO()
        code = run_cli(
            argv,
            stdout=stdout,
            model=model,
            environ=environ,
        )
        return code, json.loads(stdout.getvalue())

    def test_status_reads_sqlite_progress_as_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._sqlite_with_run(tmp)

            code, data = self._run_json(
                [
                    "--sqlite",
                    str(db_path),
                    "status",
                    "run-1",
                ]
            )

        self.assertEqual(code, 0)
        self.assertTrue(data["ok"])
        self.assertEqual(data["command"], "status")
        self.assertEqual(data["progress"]["run_id"], "run-1")
        self.assertEqual(data["progress"]["run_status"], "running")

    def test_pause_and_resume_persist_control_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._sqlite_with_run(tmp)

            pause_code, pause_data = self._run_json(
                [
                    "--sqlite",
                    str(db_path),
                    "pause",
                    "run-1",
                    "--reason",
                    "inspect",
                    "--actor",
                    "cli-test",
                ]
            )
            resume_code, resume_data = self._run_json(
                [
                    "--sqlite",
                    str(db_path),
                    "resume",
                    "run-1",
                    "continue from current state",
                    "--actor",
                    "cli-test",
                ]
            )
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
            cancel_code, cancel_data = self._run_json(
                [
                    "--sqlite",
                    str(db_path),
                    "cancel",
                    "run-1",
                    "--reason",
                    "wrong direction",
                ]
            )
            resume_code, resume_data = self._run_json(
                [
                    "--sqlite",
                    str(db_path),
                    "resume",
                    "run-1",
                    "try again",
                ]
            )

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

            code, data = self._run_json(
                [
                    "--sqlite",
                    str(db_path),
                    "fork",
                    "run-1",
                    "run-2",
                    "--reason",
                    "alternate branch",
                ]
            )
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

    def test_work_returns_structured_not_found_for_unknown_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, data = self._run_json(
                [
                    "--sqlite",
                    str(Path(tmp) / "tasks.sqlite"),
                    "work",
                    "missing-run",
                ]
            )

        self.assertEqual(code, 1)
        self.assertFalse(data["ok"])
        self.assertEqual(data["status"], "missing")
        self.assertEqual(data["error"]["type"], "not_found")

    def test_submit_work_approve_and_result_survive_process_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            db_path = Path(tmp) / "tasks.sqlite"
            model = CliPlanModel(
                {
                    "contract": {
                        "id": "model-contract",
                        "user_goal": "model must not replace host goal",
                        "acceptance_criteria": [
                            {
                                "id": "output-written",
                                "description": "output file is written",
                                "type": "tool_evidence",
                                "evidence_refs": ["output.txt"],
                            }
                        ],
                    },
                    "steps": [
                        {
                            "id": "write-output",
                            "tool_name": "write_file",
                            "arguments": {
                                "path": "output.txt",
                                "content": "durable result\n",
                            },
                            "expected_output": "output file",
                            "verification": "write evidence exists",
                            "required_evidence_refs": ["output.txt"],
                        }
                    ],
                }
            )

            submit_code, submit = self._run_json(
                [
                    "--sqlite",
                    str(db_path),
                    "--workspace",
                    str(workspace),
                    "submit",
                    "run-product",
                    "create a durable output",
                    "--model",
                    "cli-plan-model",
                    "--context-json",
                    '{"channel":"local-test"}',
                ],
                model=model,
            )
            first_work_code, first_work = self._run_json(
                [
                    "--sqlite",
                    str(db_path),
                    "--workspace",
                    str(workspace),
                    "work",
                    "run-product",
                ]
            )
            status_code, status = self._run_json(
                [
                    "--sqlite",
                    str(db_path),
                    "status",
                    "run-product",
                ]
            )
            resume_token = status["pending_interactions"][0]["resume_token"]
            approve_code, approval = self._run_json(
                [
                    "--sqlite",
                    str(db_path),
                    "--workspace",
                    str(workspace),
                    "approve",
                    "run-product",
                    "--resume-token",
                    resume_token,
                    "--feedback",
                    "approved in CLI test",
                ]
            )
            repeated_approve_code, repeated_approval = self._run_json(
                [
                    "--sqlite",
                    str(db_path),
                    "--workspace",
                    str(workspace),
                    "approve",
                    "run-product",
                    "--resume-token",
                    resume_token,
                ]
            )
            result_code, result = self._run_json(
                [
                    "--sqlite",
                    str(db_path),
                    "result",
                    "run-product",
                ]
            )
            output_content = (workspace / "output.txt").read_text()

        self.assertEqual(submit_code, 0)
        self.assertEqual(submit["submission"]["run"]["status"], "created")
        self.assertEqual(
            submit["submission"]["contract"]["id"],
            "contract_run-product",
        )
        self.assertEqual(
            submit["submission"]["plan"]["metadata"]["request_context"],
            {"channel": "local-test"},
        )
        self.assertIn('"confirmation_required": true', model.messages[1].content)
        self.assertEqual(first_work_code, 0)
        self.assertEqual(first_work["status"], "parked")
        self.assertEqual(status_code, 0)
        self.assertEqual(status["progress"]["status"], "waiting_user")
        self.assertEqual(approve_code, 0)
        self.assertTrue(approval["result"]["accepted"])
        self.assertEqual(repeated_approve_code, 1)
        self.assertIn("already resolved", repeated_approval["error"]["message"])
        self.assertEqual(result_code, 0)
        self.assertTrue(result["result"]["verified"])
        self.assertEqual(result["result"]["run"]["status"], "completed")
        self.assertEqual(result["result"]["outputs"][-1]["tool_name"], "write_file")
        self.assertEqual(output_content, "durable result\n")

    def test_submit_requires_named_api_secret_without_echoing_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, data = self._run_json(
                [
                    "--sqlite",
                    str(Path(tmp) / "tasks.sqlite"),
                    "submit",
                    "run-1",
                    "plan task",
                    "--base-url",
                    "https://provider.example/v1",
                    "--model",
                    "planner",
                    "--api-key-env",
                    "MISSING_PROVIDER_KEY",
                ],
                environ={},
            )

        self.assertEqual(code, 1)
        self.assertEqual(data["error"]["type"], "value_error")
        self.assertIn("MISSING_PROVIDER_KEY", data["error"]["message"])


if __name__ == "__main__":
    unittest.main()
