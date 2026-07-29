from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from re_zlagent.harness.agent import (  # noqa: E402
    AgentRunRequest,
    IntentDecision,
    IntentReasonCode,
    IntentRoute,
)
from re_zlagent.harness.evals import (  # noqa: E402
    IntentEvalCase,
    IntentEvalCorpus,
    IntentEvalRunner,
    default_intent_stress_corpus_path,
    load_intent_eval_corpus,
)


class SequenceIntentRouter:
    def __init__(
        self,
        *outputs: IntentDecision | Exception,
    ) -> None:
        self._outputs = list(outputs)
        self.requests: list[AgentRunRequest] = []

    async def route(self, request: AgentRunRequest) -> IntentDecision:
        self.requests.append(request)
        output = self._outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return output


class IntentEvalTests(unittest.IsolatedAsyncioTestCase):
    def _corpus(self) -> IntentEvalCorpus:
        return IntentEvalCorpus(
            schema_version=1,
            corpus_version="test.1",
            minimum_accuracy=0.66,
            cases=(
                IntentEvalCase(
                    id="chat",
                    name="chat",
                    user_input="explain",
                    expected_route=IntentRoute.CHAT,
                    language="en",
                ),
                IntentEvalCase(
                    id="task",
                    name="task",
                    user_input="read file",
                    expected_route=IntentRoute.TASK,
                    language="en",
                ),
                IntentEvalCase(
                    id="clarify",
                    name="clarify",
                    user_input="change it",
                    expected_route=IntentRoute.CLARIFY,
                    language="en",
                ),
            ),
        )

    async def test_reports_accuracy_confusion_invalid_latency_and_usage(self) -> None:
        router = SequenceIntentRouter(
            IntentDecision(
                route=IntentRoute.CHAT,
                reason_code=IntentReasonCode.DIRECT_ANSWER,
                metadata={
                    "provider": "test",
                    "model": "router-a",
                    "usage": {"input_tokens": 10, "total_tokens": 14},
                },
            ),
            IntentDecision(
                route=IntentRoute.CHAT,
                reason_code=IntentReasonCode.DIRECT_ANSWER,
                metadata={"usage": {"input_tokens": 11, "total_tokens": 15}},
            ),
            RuntimeError("provider failed"),
        )

        report = await IntentEvalRunner(router, self._corpus()).run()
        payload = report.to_dict()

        self.assertFalse(report.ok)
        self.assertEqual(report.total, 3)
        self.assertEqual(report.correct_count, 1)
        self.assertEqual(report.invalid_count, 1)
        self.assertAlmostEqual(report.accuracy, 1 / 3)
        self.assertEqual(
            payload["confusion_matrix"]["task"]["chat"],
            1,
        )
        self.assertEqual(
            payload["confusion_matrix"]["clarify"]["invalid"],
            1,
        )
        self.assertEqual(payload["aggregate"]["usage"]["input_tokens"], 21)
        self.assertEqual(payload["aggregate"]["usage"]["total_tokens"], 29)
        self.assertEqual(payload["by_language"]["en"]["total"], 3)
        self.assertEqual(payload["model"]["providers"], ["test"])
        self.assertEqual(payload["model"]["models"], ["router-a"])
        self.assertEqual(payload["results"][2]["error_type"], "RuntimeError")
        self.assertEqual(
            payload["results"][2]["error_message"],
            "provider failed",
        )
        self.assertEqual(len(router.requests), 3)

    def test_default_corpus_is_balanced_and_versioned(self) -> None:
        corpus = load_intent_eval_corpus()
        counts = {
            route: sum(
                case.expected_route is route for case in corpus.cases
            )
            for route in IntentRoute
        }

        self.assertEqual(corpus.schema_version, 1)
        self.assertEqual(corpus.corpus_version, "2026.07.1")
        self.assertEqual(len(corpus.cases), 24)
        self.assertEqual(set(counts.values()), {8})
        self.assertEqual({case.language for case in corpus.cases}, {"zh", "en"})

    def test_stress_corpus_is_separate_balanced_and_versioned(self) -> None:
        corpus = load_intent_eval_corpus(
            default_intent_stress_corpus_path()
        )
        counts = {
            route: sum(
                case.expected_route is route for case in corpus.cases
            )
            for route in IntentRoute
        }

        self.assertEqual(corpus.corpus_version, "2026.07.stress.1")
        self.assertEqual(len(corpus.cases), 24)
        self.assertEqual(set(counts.values()), {8})
        self.assertEqual(
            {
                language: sum(
                    case.language == language for case in corpus.cases
                )
                for language in {"zh", "en"}
            },
            {"zh": 12, "en": 12},
        )

    def test_loader_rejects_unknown_fields_and_missing_route_family(self) -> None:
        payload = {
            "schema_version": 1,
            "corpus_version": "test",
            "minimum_accuracy": 0.8,
            "cases": [
                {
                    "id": "chat",
                    "name": "chat",
                    "user_input": "hello",
                    "expected_route": "chat",
                    "language": "en",
                    "unexpected": True,
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "intent.json"
            path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaises(ValueError):
                load_intent_eval_corpus(path)


if __name__ == "__main__":
    unittest.main()
