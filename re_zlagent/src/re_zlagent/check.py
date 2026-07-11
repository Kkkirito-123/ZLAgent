"""Local verification entry point for re_zlagent."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Mapping, Sequence


Runner = Callable[
    [Sequence[str], Path, Mapping[str, str]],
    subprocess.CompletedProcess[str],
]


@dataclass(frozen=True, slots=True)
class CheckStep:
    """One local verification command."""

    name: str
    command: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "command", tuple(self.command))


@dataclass(frozen=True, slots=True)
class CheckStepResult:
    """Structured result for one verification command."""

    name: str
    command: tuple[str, ...]
    returncode: int
    duration_seconds: float
    stdout_tail: str = ""
    stderr_tail: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "command", tuple(self.command))

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "returncode": self.returncode,
            "duration_seconds": round(self.duration_seconds, 3),
            "command": list(self.command),
            "stdout_tail": self.stdout_tail,
            "stderr_tail": self.stderr_tail,
        }


@dataclass(frozen=True, slots=True)
class CheckReport:
    """Structured local verification report."""

    project_root: Path
    steps: tuple[CheckStepResult, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_root", Path(self.project_root))
        object.__setattr__(self, "steps", tuple(self.steps))

    @property
    def ok(self) -> bool:
        return all(step.ok for step in self.steps)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "project_root": str(self.project_root),
            "steps": [step.to_dict() for step in self.steps],
        }


def default_project_root() -> Path:
    """Return the re_zlagent workspace root from the installed source tree."""

    return Path(__file__).resolve().parents[2]


def build_steps(
    project_root: Path,
    *,
    skip_package: bool = False,
) -> tuple[CheckStep, ...]:
    """Build the default local verification steps."""

    root = Path(project_root)
    steps = [
        CheckStep(
            name="unit-tests",
            command=(
                sys.executable,
                "-W",
                "error::ResourceWarning",
                "-m",
                "unittest",
                "discover",
                "-s",
                str(root / "tests"),
            ),
        ),
        CheckStep(
            name="release-benchmarks",
            command=(
                sys.executable,
                "-m",
                "re_zlagent.benchmark",
            ),
        ),
        CheckStep(
            name="compileall",
            command=(
                sys.executable,
                "-m",
                "compileall",
                str(root / "src"),
                str(root / "tests"),
            ),
        ),
        CheckStep(
            name="lint",
            command=(
                sys.executable,
                "-m",
                "ruff",
                "check",
                "src",
                "tests",
            ),
        ),
        CheckStep(
            name="type-check",
            command=(
                sys.executable,
                "-m",
                "mypy",
                "src/re_zlagent",
            ),
        ),
        CheckStep(
            name="cli-help",
            command=(
                sys.executable,
                "-m",
                "re_zlagent.app.cli",
                "--help",
            ),
        ),
        CheckStep(
            name="benchmark-help",
            command=(
                sys.executable,
                "-m",
                "re_zlagent.benchmark",
                "--help",
            ),
        ),
    ]
    if not skip_package:
        steps.append(
            CheckStep(
                name="package-dry-run",
                command=(
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "-e",
                    ".",
                    "--dry-run",
                    "--no-deps",
                ),
            )
        )
    return tuple(steps)


def run_checks(
    *,
    project_root: Path | None = None,
    skip_package: bool = False,
    stop_on_failure: bool = True,
    cleanup: bool = True,
    runner: Runner | None = None,
) -> CheckReport:
    """Run local verification commands and return a structured report."""

    root = Path(project_root) if project_root is not None else default_project_root()
    run = runner or _run_subprocess
    env = _check_env(root)
    results: list[CheckStepResult] = []
    try:
        for step in build_steps(root, skip_package=skip_package):
            result = _run_step(step, root=root, env=env, runner=run)
            results.append(result)
            if stop_on_failure and not result.ok:
                break
        return CheckReport(project_root=root, steps=tuple(results))
    finally:
        if cleanup:
            cleanup_generated(root)


def cleanup_generated(project_root: Path) -> tuple[Path, ...]:
    """Remove local Python cache artifacts created by verification."""

    removed: list[Path] = []
    root = Path(project_root)
    for path in root.rglob("__pycache__"):
        if path.is_dir():
            shutil.rmtree(path)
            removed.append(path)
    for path in (root / "src").glob("*.egg-info"):
        if path.is_dir():
            shutil.rmtree(path)
            removed.append(path)
    for name in (".mypy_cache", ".ruff_cache"):
        path = root / name
        if path.is_dir():
            shutil.rmtree(path)
            removed.append(path)
    return tuple(removed)


def build_parser() -> argparse.ArgumentParser:
    """Build the check CLI parser."""

    parser = argparse.ArgumentParser(prog="re-zlagent-check")
    parser.add_argument(
        "--project-root",
        type=Path,
        default=None,
        help="Path to the re_zlagent workspace root.",
    )
    parser.add_argument(
        "--skip-package",
        action="store_true",
        help="Skip pip editable dry-run metadata validation.",
    )
    parser.add_argument(
        "--keep-going",
        action="store_true",
        help="Continue running later checks after a failure.",
    )
    parser.add_argument(
        "--no-cleanup",
        action="store_true",
        help="Keep generated __pycache__ and egg-info artifacts.",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """Run local verification and exit with a process-style status."""

    args = build_parser().parse_args(list(argv) if argv is not None else None)
    report = run_checks(
        project_root=args.project_root,
        skip_package=args.skip_package,
        stop_on_failure=not args.keep_going,
        cleanup=not args.no_cleanup,
    )
    print(
        json.dumps(
            report.to_dict(),
            ensure_ascii=False,
            indent=2 if args.pretty else None,
            sort_keys=True,
        )
    )
    raise SystemExit(0 if report.ok else 1)


def _run_step(
    step: CheckStep,
    *,
    root: Path,
    env: Mapping[str, str],
    runner: Runner,
) -> CheckStepResult:
    start = perf_counter()
    completed = runner(step.command, root, env)
    duration = perf_counter() - start
    return CheckStepResult(
        name=step.name,
        command=step.command,
        returncode=completed.returncode,
        duration_seconds=duration,
        stdout_tail=_tail(completed.stdout),
        stderr_tail=_tail(completed.stderr),
    )


def _run_subprocess(
    command: Sequence[str],
    root: Path,
    env: Mapping[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=root,
        env=dict(env),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def _check_env(project_root: Path) -> dict[str, str]:
    env = dict(os.environ)
    src = str(project_root / "src")
    current = env.get("PYTHONPATH")
    env["PYTHONPATH"] = src if not current else f"{src}{os.pathsep}{current}"
    return env


def _tail(value: str | None, *, limit: int = 4000) -> str:
    if not value:
        return ""
    return value[-limit:]


if __name__ == "__main__":
    main()
