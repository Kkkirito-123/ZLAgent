from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from re_zlagent.benchmark import run_benchmark_cli  # noqa: E402
from re_zlagent.harness.evals import (  # noqa: E402
    BenchmarkCaseResult,
    BenchmarkCorpus,
    BenchmarkSuite,
    ReleaseBenchmarkReport,
    ReleaseBenchmarkRunner,
    load_benchmark_corpus,
)
from re_zlagent.harness.tasking import TaskRunStatus  # noqa: E402


class ReleaseBenchmarkTests(unittest.IsolatedAsyncioTestCase):
    def test_default_corpus_is_versioned_and_covers_required_suites(self) -> None:
        corpus = load_benchmark_corpus()

        self.assertEqual(corpus.schema_version, 1)
        self.assertEqual(corpus.corpus_version, "2026.07.1")
        self.assertEqual({case.suite for case in corpus.cases}, set(BenchmarkSuite))
        self.assertEqual(len(corpus.cases), 6)

    async def test_release_corpus_passes_all_semantic_and_recovery_gates(self) -> None:
        report = await ReleaseBenchmarkRunner(load_benchmark_corpus()).run()

        self.assertTrue(report.ok)
        self.assertEqual(report.pass_rate, 1.0)
        self.assertEqual(report.false_completions, 0)
        self.assertEqual(report.duplicate_side_effects, 0)
        self.assertEqual(report.abandoned_runs, 0)
        self.assertEqual(report.violations, ())

    async def test_per_case_latency_budget_is_a_blocking_expectation(self) -> None:
        corpus = load_benchmark_corpus()
        cases = (
            replace(corpus.cases[0], max_duration_ms=0.000001),
            *corpus.cases[1:],
        )
        strict = BenchmarkCorpus(
            schema_version=corpus.schema_version,
            corpus_version=corpus.corpus_version,
            thresholds=corpus.thresholds,
            cases=cases,
        )

        report = await ReleaseBenchmarkRunner(strict).run()

        self.assertFalse(report.ok)
        self.assertIn("duration exceeded budget", report.results[0].failures[0])

    def test_report_fails_closed_when_global_threshold_is_exceeded(self) -> None:
        corpus = load_benchmark_corpus()
        result = BenchmarkCaseResult(
            case_id="regression",
            name="regression",
            suite="resilience",
            kind="synthetic",
            passed=True,
            duration_ms=1,
            accepted=True,
            status=TaskRunStatus.COMPLETED,
            recovered=False,
            metrics={
                "false_completions": 1,
                "duplicate_side_effects": 1,
                "abandoned_runs": 1,
            },
        )
        report = ReleaseBenchmarkReport(
            corpus_version=corpus.corpus_version,
            thresholds=corpus.thresholds,
            results=(result,),
            duration_ms=1,
        )

        self.assertFalse(report.ok)
        self.assertEqual(len(report.violations), 3)

    def test_loader_rejects_corpus_without_all_required_suites(self) -> None:
        source = json.loads(
            (ROOT / "src/re_zlagent/harness/evals/corpora/release-v1.json").read_text()
        )
        source["cases"] = [source["cases"][0]]
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "bad.json"
            path.write_text(json.dumps(source))

            with self.assertRaisesRegex(ValueError, "missing required suites"):
                load_benchmark_corpus(path)

    def test_benchmark_cli_returns_machine_readable_report(self) -> None:
        stdout = io.StringIO()

        code = run_benchmark_cli([], stdout=stdout)
        data = json.loads(stdout.getvalue())

        self.assertEqual(code, 0)
        self.assertTrue(data["ok"])
        self.assertEqual(data["aggregate"]["total"], 6)


if __name__ == "__main__":
    unittest.main()
