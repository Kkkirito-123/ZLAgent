from __future__ import annotations

import asyncio
import json
import io
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from re_zlagent.harness.evals import (  # noqa: E402
    CheckOperator,
    EffectivenessBenchmarkRunner,
    EffectivenessCase,
    EffectivenessCheck,
    EffectivenessCorpus,
    EffectivenessExecutorKind,
    EffectivenessManifest,
    EffectivenessSplit,
    EffectivenessTrack,
    ProjectEffectivenessExecutors,
    default_effectiveness_manifest_path,
    load_effectiveness_corpus,
)
from re_zlagent.effectiveness_benchmark import run_effectiveness_cli  # noqa: E402
from re_zlagent.harness.model import ModelMessage, ModelResponse  # noqa: E402


class StaticTaskModel:
    async def complete(
        self,
        messages: tuple[ModelMessage, ...],
    ) -> ModelResponse:
        del messages
        return ModelResponse(
            content=json.dumps(
                {
                    "contract": {
                        "id": "proposed",
                        "user_goal": "核对资料",
                        "acceptance_criteria": [
                            {
                                "id": "evidence",
                                "description": "资料证据存在",
                                "type": "tool_evidence",
                                "evidence_refs": ["profile:user"],
                            }
                        ],
                    },
                    "steps": [
                        {
                            "id": "collect-profile",
                            "tool_name": "collect_evidence",
                            "arguments": {"ref": "profile:user"},
                            "required_evidence_refs": ["profile:user"],
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            raw={
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 50,
                    "total_tokens": 150,
                }
            },
        )


class EffectivenessCorpusTests(unittest.TestCase):
    def test_packaged_pilot_is_chinese_versioned_and_covers_all_tracks(self) -> None:
        corpus = load_effectiveness_corpus()
        counts = {
            track: sum(case.track is track for case in corpus.cases)
            for track in EffectivenessTrack
        }

        self.assertEqual(corpus.manifest.schema_version, 1)
        self.assertEqual(corpus.manifest.corpus_version, "2026.07.zh.pilot.1")
        self.assertEqual(corpus.manifest.language, "zh-CN")
        self.assertEqual(corpus.manifest.status, "pilot")
        self.assertEqual(len(corpus.cases), 70)
        self.assertEqual(
            counts,
            {
                EffectivenessTrack.INTENT_ROUTING: 9,
                EffectivenessTrack.HARNESS_RELIABILITY: 23,
                EffectivenessTrack.MEMORY_CONTEXT: 19,
                EffectivenessTrack.DAG_TOKEN: 7,
                EffectivenessTrack.AGENT_TASK: 12,
            },
        )
        self.assertTrue(
            all(case.split is EffectivenessSplit.PILOT for case in corpus.cases)
        )
        self.assertTrue(all(case.checks for case in corpus.cases))
        self.assertTrue(all(case.capabilities for case in corpus.cases))
        covered = {
            capability for case in corpus.cases for capability in case.capabilities
        }
        self.assertEqual(
            set(corpus.manifest.required_capabilities).difference(covered),
            set(),
        )

    def test_jsonl_error_reports_exact_line(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cases_path = Path(temporary) / "bad.jsonl"
            cases_path.write_text("\n{not-json}\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "line 2"):
                load_effectiveness_corpus(
                    default_effectiveness_manifest_path(),
                    cases_path,
                )

    def test_case_loader_rejects_unknown_fields(self) -> None:
        source = json.loads(
            (ROOT / "src/re_zlagent/harness/evals/corpora/effectiveness-v1.cases.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()[0]
        )
        source["unexpected"] = True
        with tempfile.TemporaryDirectory() as temporary:
            cases_path = Path(temporary) / "bad.jsonl"
            cases_path.write_text(
                json.dumps(source, ensure_ascii=False),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "unsupported fields"):
                load_effectiveness_corpus(
                    default_effectiveness_manifest_path(),
                    cases_path,
                )


class EffectivenessRunnerTests(unittest.IsolatedAsyncioTestCase):
    def _corpus(self, *, repetitions: int = 2) -> EffectivenessCorpus:
        return EffectivenessCorpus(
            manifest=EffectivenessManifest(
                schema_version=1,
                corpus_version="test.zh.1",
                language="zh-CN",
                status="pilot",
                description_zh="用于验证效果评分器的中文测试语料。",
                required_tracks=(EffectivenessTrack.INTENT_ROUTING,),
                minimum_cases_by_track={EffectivenessTrack.INTENT_ROUTING: 1},
                required_capabilities=("intent_routing",),
            ),
            cases=(
                EffectivenessCase(
                    schema_version=1,
                    corpus_version="test.zh.1",
                    id="route-chat",
                    track=EffectivenessTrack.INTENT_ROUTING,
                    executor=EffectivenessExecutorKind.INTENT_ROUTER,
                    split=EffectivenessSplit.PILOT,
                    name_zh="普通对话路由",
                    scenario_zh="用户只要求解释概念，不需要执行工具。",
                    repetitions=repetitions,
                    input={"user_input": "请解释 MCP。"},
                    checks=(
                        EffectivenessCheck(
                            path="route",
                            operator=CheckOperator.EQ,
                            value="chat",
                            description_zh="请求应进入普通对话",
                        ),
                        EffectivenessCheck(
                            path="usage.total_tokens",
                            operator=CheckOperator.LTE,
                            value=100,
                            description_zh="总 Token 不得超过预算",
                        ),
                    ),
                    capabilities=("intent_routing",),
                ),
            ),
        )

    async def test_runner_repeats_checks_and_serializes_jsonl_and_markdown(
        self,
    ) -> None:
        observed_repetitions: list[int] = []

        async def executor(
            case: EffectivenessCase,
            repetition: int,
        ) -> dict[str, Any]:
            self.assertEqual(case.id, "route-chat")
            observed_repetitions.append(repetition)
            return {"route": "chat", "usage": {"total_tokens": 42}}

        report = await EffectivenessBenchmarkRunner(
            self._corpus(),
            executors={EffectivenessExecutorKind.INTENT_ROUTER: executor},
        ).run()

        self.assertTrue(report.ok)
        self.assertEqual(report.pass_rate, 1.0)
        self.assertEqual(observed_repetitions, [1, 2])
        self.assertEqual(len(report.to_jsonl().splitlines()), 2)
        self.assertIn("通过率：100.00%", report.to_markdown())
        self.assertEqual(
            report.to_dict()["by_track"]["intent_routing"]["passed"],
            2,
        )

    async def test_missing_executor_fails_closed(self) -> None:
        report = await EffectivenessBenchmarkRunner(
            self._corpus(repetitions=1),
            executors={},
        ).run()

        self.assertFalse(report.ok)
        self.assertEqual(report.results[0].error_type, "missing_executor")
        self.assertIn("缺少执行适配器", report.results[0].failures[0])

    async def test_packaged_deterministic_tracks_execute_real_boundaries(self) -> None:
        loop = asyncio.get_running_loop()
        previous_debug = loop.get_debug()
        loop.set_debug(False)
        try:
            report = await EffectivenessBenchmarkRunner(
                load_effectiveness_corpus(),
                executors=ProjectEffectivenessExecutors().as_mapping(),
            ).run(
                tracks=(
                    EffectivenessTrack.HARNESS_RELIABILITY,
                    EffectivenessTrack.MEMORY_CONTEXT,
                    EffectivenessTrack.DAG_TOKEN,
                )
            )
        finally:
            loop.set_debug(previous_debug)

        self.assertTrue(report.ok, report.to_markdown())
        self.assertEqual(len(report.results), 66)
        self.assertTrue(
            all(
                result.track is not EffectivenessTrack.INTENT_ROUTING
                for result in report.results
            )
        )

    async def test_agent_task_completion_requires_runtime_evidence(self) -> None:
        packaged = load_effectiveness_corpus()
        case = next(
            item for item in packaged.cases if item.id == "agent-task-profile-one-step"
        )
        corpus = EffectivenessCorpus(
            manifest=EffectivenessManifest(
                schema_version=1,
                corpus_version=packaged.manifest.corpus_version,
                language="zh-CN",
                status="pilot",
                description_zh="验证真实模型任务必须经过工具证据和运行时验收。",
                required_tracks=(EffectivenessTrack.AGENT_TASK,),
                minimum_cases_by_track={EffectivenessTrack.AGENT_TASK: 1},
                required_capabilities=case.capabilities,
            ),
            cases=(case,),
        )

        report = await EffectivenessBenchmarkRunner(
            corpus,
            executors=ProjectEffectivenessExecutors(
                task_model=StaticTaskModel()
            ).as_mapping(),
        ).run()

        self.assertTrue(report.ok, report.to_markdown())
        self.assertEqual(report.task_completion_rate, 1.0)
        self.assertEqual(report.false_completions, 0)
        self.assertEqual(report.agent_task_token_usage["total_tokens"], 150)


class EffectivenessCliTests(unittest.TestCase):
    def test_validate_only_reports_case_and_observation_counts(self) -> None:
        stdout = io.StringIO()

        code = run_effectiveness_cli(["--validate-only"], stdout=stdout)
        payload = json.loads(stdout.getvalue())

        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["validated_only"])
        self.assertEqual(payload["language"], "zh-CN")
        self.assertEqual(payload["aggregate"], {"cases": 70, "observations": 105})
        self.assertEqual(
            payload["capability_coverage"]["real_model_task_execution"],
            12,
        )
        self.assertFalse(
            [
                capability
                for capability, count in payload["capability_coverage"].items()
                if count == 0
            ]
        )

    def test_full_run_requires_real_model_configuration(self) -> None:
        stdout = io.StringIO()

        code = run_effectiveness_cli([], stdout=stdout, environ={})
        payload = json.loads(stdout.getvalue())

        self.assertEqual(code, 2)
        self.assertFalse(payload["ok"])
        self.assertIn("ZLAGENT_MODEL_BASE_URL", payload["error"]["message"])


if __name__ == "__main__":
    unittest.main()
