from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from re_zlagent.check import build_steps, cleanup_generated, run_checks  # noqa: E402


class CheckCommandTests(unittest.TestCase):
    def test_quality_workflow_runs_the_same_release_gate(self) -> None:
        workflow = (ROOT / ".github/workflows/quality.yml").read_text()

        self.assertIn('python-version: ["3.11", "3.13"]', workflow)
        self.assertIn("python -m pip install -e '.[dev]'", workflow)
        self.assertIn("python -m re_zlagent.check", workflow)

    def test_build_steps_can_skip_package_metadata_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            full = build_steps(root)
            quick = build_steps(root, skip_package=True)

        self.assertEqual(
            [step.name for step in full],
            [
                "unit-tests",
                "release-benchmarks",
                "compileall",
                "lint",
                "type-check",
                "cli-help",
                "benchmark-help",
                "intent-eval-help",
                "effectiveness-benchmark-help",
                "package-dry-run",
            ],
        )
        self.assertEqual(
            [step.name for step in quick],
            [
                "unit-tests",
                "release-benchmarks",
                "compileall",
                "lint",
                "type-check",
                "cli-help",
                "benchmark-help",
                "intent-eval-help",
                "effectiveness-benchmark-help",
            ],
        )

    def test_run_checks_sets_pythonpath_and_returns_structured_report(self) -> None:
        calls: list[tuple[tuple[str, ...], Path, str]] = []

        def runner(
            command: tuple[str, ...],
            root: Path,
            env: dict[str, str],
        ) -> subprocess.CompletedProcess[str]:
            calls.append((tuple(command), root, env["PYTHONPATH"]))
            return subprocess.CompletedProcess(
                args=list(command),
                returncode=0,
                stdout="ok",
                stderr="",
            )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "tests").mkdir()

            report = run_checks(
                project_root=root,
                skip_package=True,
                cleanup=False,
                runner=runner,
            )

        self.assertTrue(report.ok)
        self.assertEqual(
            [step.name for step in report.steps],
            [
                "unit-tests",
                "release-benchmarks",
                "compileall",
                "lint",
                "type-check",
                "cli-help",
                "benchmark-help",
                "intent-eval-help",
                "effectiveness-benchmark-help",
            ],
        )
        self.assertEqual(len(calls), 9)
        for _, call_root, pythonpath in calls:
            self.assertEqual(call_root, root)
            self.assertEqual(pythonpath.split(os.pathsep)[0], str(root / "src"))
        self.assertTrue(report.to_dict()["ok"])

    def test_run_checks_stops_on_first_failure_by_default(self) -> None:
        seen: list[tuple[str, ...]] = []

        def runner(
            command: tuple[str, ...],
            root: Path,
            env: dict[str, str],
        ) -> subprocess.CompletedProcess[str]:
            seen.append(tuple(command))
            return subprocess.CompletedProcess(
                args=list(command),
                returncode=1 if len(seen) == 2 else 0,
                stdout="",
                stderr="failed",
            )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "tests").mkdir()

            report = run_checks(
                project_root=root,
                skip_package=True,
                cleanup=False,
                runner=runner,
            )

        self.assertFalse(report.ok)
        self.assertEqual(
            [step.name for step in report.steps],
            [
                "unit-tests",
                "release-benchmarks",
            ],
        )
        self.assertEqual(report.steps[-1].stderr_tail, "failed")

    def test_cleanup_generated_removes_python_cache_and_egg_info(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "tests" / "__pycache__"
            egg_info = root / "src" / "re_zlagent.egg-info"
            mypy_cache = root / ".mypy_cache"
            ruff_cache = root / ".ruff_cache"
            cache.mkdir(parents=True)
            egg_info.mkdir(parents=True)
            mypy_cache.mkdir(parents=True)
            ruff_cache.mkdir(parents=True)

            removed = cleanup_generated(root)

            self.assertFalse(cache.exists())
            self.assertFalse(egg_info.exists())
            self.assertFalse(mypy_cache.exists())
            self.assertFalse(ruff_cache.exists())
            self.assertEqual(
                set(removed),
                {cache, egg_info, mypy_cache, ruff_cache},
            )


if __name__ == "__main__":
    unittest.main()
