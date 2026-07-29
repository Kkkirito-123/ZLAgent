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
from re_zlagent.harness.evals import (  # noqa: E402
    default_intent_stress_corpus_path,
    load_intent_eval_corpus,
)
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


class CliResponseQueueModel:
    def __init__(self, *responses: str) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[ModelMessage, ...]] = []

    async def complete(
        self,
        messages: tuple[ModelMessage, ...],
    ) -> ModelResponse:
        self.calls.append(messages)
        if not self._responses:
            raise AssertionError("unexpected model call")
        return ModelResponse(
            content=self._responses.pop(0),
            raw={"provider": "test", "model": "cli-general-model"},
        )


def _intent_response(route: str) -> str:
    reason_by_route = {
        "chat": "direct_answer",
        "task": "external_action",
        "clarify": "missing_details",
    }
    return json.dumps(
        {
            "route": route,
            "reason_code": reason_by_route[route],
            "clarification_question": (
                "请补充执行目标和必要信息。"
                if route == "clarify"
                else None
            ),
        }
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

    def test_branches_reads_nested_lineage_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._sqlite_with_run(tmp)
            self._run_json(
                [
                    "--sqlite",
                    str(db_path),
                    "fork",
                    "run-1",
                    "run-2",
                ]
            )
            self._run_json(
                [
                    "--sqlite",
                    str(db_path),
                    "fork",
                    "run-2",
                    "run-3",
                ]
            )

            code, data = self._run_json(
                [
                    "--sqlite",
                    str(db_path),
                    "branches",
                    "run-3",
                ]
            )

        self.assertEqual(code, 0)
        self.assertEqual(data["tree"]["root_run_id"], "run-1")
        self.assertEqual(data["tree"]["node_count"], 3)
        self.assertTrue(
            data["tree"]["root"]["children"][0]["children"][0]["selected"]
        )

    def test_missing_sqlite_returns_json_argument_error(self) -> None:
        code, data = self._run_json(["status", "run-1"])

        self.assertEqual(code, 2)
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"]["type"], "invalid_arguments")
        self.assertIn("--sqlite", data["error"]["message"])

    def test_ask_chat_runs_without_sqlite_and_is_not_verified(self) -> None:
        model = CliResponseQueueModel("你好，这是通用 Agent 的直接回答。")

        code, data = self._run_json(
            [
                "ask",
                "你好",
                "--mode",
                "chat",
                "--run-id",
                "ask-chat-1",
                "--context-json",
                '{"surface":"cli-test"}',
            ],
            model=model,
        )

        self.assertEqual(code, 0)
        self.assertTrue(data["ok"])
        self.assertEqual(data["command"], "ask")
        self.assertEqual(data["mode"], "chat")
        self.assertEqual(data["run_id"], "ask-chat-1")
        self.assertFalse(data["verified"])
        self.assertIsNone(data["task"])
        self.assertEqual(data["response"], "你好，这是通用 Agent 的直接回答。")
        self.assertIn('"surface": "cli-test"', model.calls[0][1].content)
        self.assertIsNotNone(data["context_manifest"])
        self.assertTrue(
            all(
                "content" not in segment
                for segment in data["context_manifest"]["segments"]
            )
        )

    def test_ask_auto_routes_chat_and_gates_task_execution(self) -> None:
        chat_model = CliResponseQueueModel(
            _intent_response("chat"),
            "auto chat response",
        )
        task_model = CliResponseQueueModel(_intent_response("task"))

        chat_code, chat = self._run_json(
            ["ask", "解释一下 checkpoint", "--mode", "auto"],
            model=chat_model,
        )
        task_code, task = self._run_json(
            ["ask", "读取 README.md", "--mode", "auto"],
            model=task_model,
        )

        self.assertEqual(chat_code, 0)
        self.assertEqual(chat["mode"], "chat")
        self.assertEqual(chat["intent"]["route"], "chat")
        self.assertEqual(chat["response"], "auto chat response")
        self.assertEqual(len(chat_model.calls), 2)
        self.assertEqual(task_code, 0)
        self.assertEqual(task["mode"], "task")
        self.assertFalse(task["verified"])
        self.assertIsNone(task["task"])
        self.assertEqual(
            task["response_metadata"]["response_source"],
            "intent_router_gate",
        )
        self.assertEqual(len(task_model.calls), 1)

    def test_explicit_memory_persists_and_is_recalled_after_cli_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tasks.sqlite"
            first_model = CliResponseQueueModel()
            remember_code, remembered = self._run_json(
                [
                    "--sqlite",
                    str(db_path),
                    "ask",
                    "请记住：我喜欢简洁的报告。",
                    "--mode",
                    "chat",
                ],
                model=first_model,
            )
            second_model = CliResponseQueueModel("以后会保持简洁。")
            recall_code, recalled = self._run_json(
                [
                    "--sqlite",
                    str(db_path),
                    "ask",
                    "以后报告怎么写？",
                    "--mode",
                    "chat",
                ],
                model=second_model,
            )

        self.assertEqual(remember_code, 0)
        self.assertTrue(remembered["memory_capture"]["written"])
        self.assertEqual(first_model.calls, [])
        self.assertEqual(recall_code, 0)
        self.assertEqual(recalled["response"], "以后会保持简洁。")
        self.assertIn("我喜欢简洁的报告", second_model.calls[0][1].content)

    def test_ask_task_executes_runtime_and_returns_verified_answer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "note.txt").write_text(
                "General Agent runtime effect.",
                encoding="utf-8",
            )
            db_path = root / "tasks.sqlite"
            plan = {
                "contract": {
                    "id": "model-contract",
                    "user_goal": "model goal",
                    "acceptance_criteria": [
                        {
                            "id": "note-read",
                            "description": "note is read",
                            "type": "tool_evidence",
                            "evidence_refs": ["note.txt"],
                        }
                    ],
                },
                "steps": [
                    {
                        "id": "read-note",
                        "tool_name": "read_file",
                        "arguments": {"path": "note.txt"},
                        "required_evidence_refs": ["note.txt"],
                    }
                ],
            }
            model = CliResponseQueueModel(
                json.dumps(plan),
                "效果验证：已通过 Runtime 读取 note.txt。",
            )

            code, data = self._run_json(
                [
                    "--sqlite",
                    str(db_path),
                    "--workspace",
                    str(workspace),
                    "ask",
                    "读取 note.txt 并说明结果",
                    "--mode",
                    "task",
                    "--run-id",
                    "ask-task-1",
                ],
                model=model,
            )
            store = SqliteTaskStore(db_path)
            run = store.get_run("ask-task-1")
            store.close()

        self.assertEqual(code, 0)
        self.assertTrue(data["ok"])
        self.assertEqual(data["mode"], "task")
        self.assertTrue(data["verified"])
        self.assertEqual(
            data["token_usage"],
            {"aggregate": {}, "phases": {}},
        )
        self.assertEqual(data["response"], "效果验证：已通过 Runtime 读取 note.txt。")
        self.assertEqual(data["task"]["status"], "completed")
        self.assertTrue(data["task"]["accepted"])
        self.assertEqual(data["task"]["tool_result_count"], 1)
        self.assertEqual(
            data["task"]["acceptance"]["evidence_refs"],
            ["note.txt"],
        )
        self.assertNotIn("events", data["task"])
        self.assertNotIn(str(workspace), json.dumps(data))
        self.assertEqual(run.status, TaskRunStatus.COMPLETED)
        self.assertEqual(len(model.calls), 2)
        self.assertIn("General Agent runtime effect.", model.calls[1][1].content)

    def test_ask_requires_explicit_model_configuration(self) -> None:
        code, data = self._run_json(["ask", "hello"], environ={})

        self.assertEqual(code, 2)
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"]["type"], "invalid_arguments")
        self.assertIn("ZLAGENT_MODEL_BASE_URL", data["error"]["message"])

    def test_model_output_ceiling_environment_must_be_positive_integer(self) -> None:
        code, data = self._run_json(
            ["intent-eval", "--no-api-key"],
            environ={
                "ZLAGENT_MODEL_BASE_URL": "http://localhost:1/v1",
                "ZLAGENT_MODEL_NAME": "local",
                "ZLAGENT_MODEL_MAX_TOKENS": "invalid",
            },
        )

        self.assertEqual(code, 2)
        self.assertEqual(data["error"]["type"], "invalid_arguments")
        self.assertIn("max output tokens", data["error"]["message"])

    def test_intent_eval_reports_seed_corpus_accuracy_without_sqlite(self) -> None:
        corpus = load_intent_eval_corpus()
        model = CliResponseQueueModel(
            *[
                _intent_response(case.expected_route.value)
                for case in corpus.cases
            ]
        )

        code, data = self._run_json(
            ["intent-eval"],
            model=model,
        )

        self.assertEqual(code, 0)
        self.assertTrue(data["ok"])
        self.assertEqual(data["command"], "intent-eval")
        self.assertEqual(data["corpus_version"], "2026.07.1")
        self.assertEqual(data["aggregate"]["total"], 24)
        self.assertEqual(data["aggregate"]["correct"], 24)
        self.assertEqual(data["aggregate"]["accuracy"], 1.0)
        self.assertEqual(data["aggregate"]["invalid"], 0)
        self.assertEqual(data["by_route"]["chat"]["accuracy"], 1.0)
        self.assertEqual(data["by_route"]["task"]["accuracy"], 1.0)
        self.assertEqual(data["by_route"]["clarify"]["accuracy"], 1.0)
        self.assertEqual(len(model.calls), 24)

    def test_intent_eval_returns_nonzero_below_accuracy_target(self) -> None:
        model = CliResponseQueueModel(
            *[_intent_response("chat") for _ in range(24)]
        )

        code, data = self._run_json(
            ["intent-eval"],
            model=model,
        )

        self.assertEqual(code, 1)
        self.assertFalse(data["ok"])
        self.assertEqual(data["aggregate"]["correct"], 8)
        self.assertAlmostEqual(data["aggregate"]["accuracy"], 1 / 3)
        self.assertEqual(data["confusion_matrix"]["task"]["chat"], 8)
        self.assertEqual(data["confusion_matrix"]["clarify"]["chat"], 8)

    def test_intent_eval_stress_uses_separate_packaged_corpus(self) -> None:
        corpus = load_intent_eval_corpus(
            default_intent_stress_corpus_path()
        )
        model = CliResponseQueueModel(
            *[
                _intent_response(case.expected_route.value)
                for case in corpus.cases
            ]
        )

        code, data = self._run_json(
            ["intent-eval", "--stress"],
            model=model,
        )

        self.assertEqual(code, 0)
        self.assertEqual(data["corpus_version"], "2026.07.stress.1")
        self.assertEqual(data["aggregate"]["total"], 24)
        self.assertEqual(data["aggregate"]["correct"], 24)

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

    def test_skill_install_requires_approval_and_survives_cli_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "tasks.sqlite"
            imports = root / "skill-imports"
            managed = root / "skills"
            candidate = imports / "demo"
            candidate.mkdir(parents=True)
            managed.mkdir()
            (candidate / "SKILL.md").write_text(
                "---\n"
                "id: demo-skill\n"
                "name: Demo Skill\n"
                "version: 1.0.0\n"
                "---\n"
                "Use bounded evidence.\n",
                encoding="utf-8",
            )
            model = CliPlanModel(
                {
                    "contract": {
                        "id": "model-contract",
                        "user_goal": "install a controlled local skill",
                        "acceptance_criteria": [
                            {
                                "id": "skill-installed",
                                "description": "skill package is installed",
                                "type": "tool_evidence",
                                "evidence_refs": ["skill:demo-skill"],
                            }
                        ],
                    },
                    "steps": [
                        {
                            "id": "install-demo-skill",
                            "tool_name": "install_skill",
                            "arguments": {
                                "source_path": "demo",
                                "skill_id": "demo-skill",
                            },
                            "expected_output": "installed Skill",
                            "verification": "installation evidence exists",
                            "required_evidence_refs": ["skill:demo-skill"],
                        }
                    ],
                }
            )
            capability_args = [
                "--skill-import-dir",
                str(imports),
                "--skills-dir",
                str(managed),
            ]

            submit_code, _ = self._run_json(
                [
                    "--sqlite",
                    str(db_path),
                    *capability_args,
                    "submit",
                    "run-skill-install",
                    "install the demo Skill",
                    "--model",
                    "cli-plan-model",
                ],
                model=model,
            )
            work_code, work = self._run_json(
                [
                    "--sqlite",
                    str(db_path),
                    *capability_args,
                    "work",
                    "run-skill-install",
                ]
            )
            _, status = self._run_json(
                ["--sqlite", str(db_path), "status", "run-skill-install"]
            )
            installed_before_approval = (managed / "demo-skill").exists()
            token = status["pending_interactions"][0]["resume_token"]
            approve_code, approval = self._run_json(
                [
                    "--sqlite",
                    str(db_path),
                    *capability_args,
                    "approve",
                    "run-skill-install",
                    "--resume-token",
                    token,
                    "--feedback",
                    "approved local Skill package",
                ]
            )
            result_code, result = self._run_json(
                ["--sqlite", str(db_path), "result", "run-skill-install"]
            )

            self.assertEqual(submit_code, 0)
            self.assertEqual(work_code, 0)
            self.assertEqual(work["status"], "parked")
            self.assertFalse(installed_before_approval)
            self.assertEqual(approve_code, 0)
            self.assertTrue(approval["result"]["accepted"])
            self.assertEqual(result_code, 0)
            self.assertTrue(result["result"]["verified"])
            self.assertEqual(
                result["result"]["outputs"][-1]["tool_name"],
                "install_skill",
            )
            self.assertTrue((managed / "demo-skill" / "SKILL.md").is_file())


if __name__ == "__main__":
    unittest.main()
