"""Versioned release benchmark corpus schema and loader."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from re_zlagent.harness.tasking import TaskRunStatus


CORPUS_SCHEMA_VERSION = 1


class BenchmarkSuite(str, Enum):
    """Stable release benchmark suite groups."""

    SHORT_PLAN = "short_plan"
    LONG_TASK = "long_task"
    RESILIENCE = "resilience"


class BenchmarkKind(str, Enum):
    """Built-in deterministic benchmark scenario implementations."""

    VERIFIED_SUCCESS = "verified_success"
    FALSE_COMPLETION_GUARD = "false_completion_guard"
    RETRY_CONTINUATION = "retry_continuation"
    OUTBOX_CRASH_REPLAY = "outbox_crash_replay"
    SQLITE_APPROVAL_RESTART = "sqlite_approval_restart"
    EXPIRED_LEASE_RECLAIM = "expired_lease_reclaim"


MetricValue = bool | int | float | str


@dataclass(frozen=True, slots=True)
class BenchmarkExpectation:
    """Expected observable output for one benchmark case."""

    accepted: bool
    status: TaskRunStatus
    recovered: bool
    metrics: dict[str, MetricValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metrics", dict(self.metrics))


@dataclass(frozen=True, slots=True)
class BenchmarkCase:
    """One versioned release benchmark case."""

    id: str
    name: str
    suite: BenchmarkSuite
    kind: BenchmarkKind
    max_duration_ms: float
    expected: BenchmarkExpectation

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("benchmark case id must be non-empty")
        if not self.name.strip():
            raise ValueError("benchmark case name must be non-empty")
        if self.max_duration_ms <= 0:
            raise ValueError("benchmark case max_duration_ms must be positive")


@dataclass(frozen=True, slots=True)
class BenchmarkThresholds:
    """Release-wide semantic and latency thresholds."""

    minimum_pass_rate: float
    maximum_false_completions: int
    maximum_duplicate_side_effects: int
    maximum_abandoned_runs: int
    maximum_suite_duration_ms: float

    def __post_init__(self) -> None:
        if self.minimum_pass_rate < 0 or self.minimum_pass_rate > 1:
            raise ValueError("minimum_pass_rate must be between 0 and 1")
        for name in (
            "maximum_false_completions",
            "maximum_duplicate_side_effects",
            "maximum_abandoned_runs",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be >= 0")
        if self.maximum_suite_duration_ms <= 0:
            raise ValueError("maximum_suite_duration_ms must be positive")

    def to_dict(self) -> dict[str, MetricValue]:
        return {
            "minimum_pass_rate": self.minimum_pass_rate,
            "maximum_false_completions": self.maximum_false_completions,
            "maximum_duplicate_side_effects": self.maximum_duplicate_side_effects,
            "maximum_abandoned_runs": self.maximum_abandoned_runs,
            "maximum_suite_duration_ms": self.maximum_suite_duration_ms,
        }


@dataclass(frozen=True, slots=True)
class BenchmarkCorpus:
    """Validated persistent corpus plus release thresholds."""

    schema_version: int
    corpus_version: str
    thresholds: BenchmarkThresholds
    cases: tuple[BenchmarkCase, ...]

    def __post_init__(self) -> None:
        if self.schema_version != CORPUS_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported benchmark schema version: {self.schema_version}"
            )
        if not self.corpus_version.strip():
            raise ValueError("corpus_version must be non-empty")
        object.__setattr__(self, "cases", tuple(self.cases))
        if not self.cases:
            raise ValueError("benchmark corpus must contain cases")
        ids = [case.id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("benchmark case ids must be unique")
        present_suites = {case.suite for case in self.cases}
        required_suites = set(BenchmarkSuite)
        missing = sorted(item.value for item in required_suites - present_suites)
        if missing:
            raise ValueError(
                "benchmark corpus is missing required suites: " + ", ".join(missing)
            )


def default_corpus_path() -> Path:
    """Return the release corpus shipped with the eval package."""

    return Path(__file__).with_name("corpora") / "release-v1.json"


def load_benchmark_corpus(path: Path | str | None = None) -> BenchmarkCorpus:
    """Load and strictly validate one JSON benchmark corpus."""

    source = Path(path) if path is not None else default_corpus_path()
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read benchmark corpus {source}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"benchmark corpus is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("benchmark corpus root must be an object")

    thresholds = _parse_thresholds(_require_object(raw, "thresholds"))
    cases_raw = raw.get("cases")
    if not isinstance(cases_raw, list):
        raise ValueError("benchmark corpus cases must be a list")
    cases = tuple(_parse_case(item) for item in cases_raw)
    return BenchmarkCorpus(
        schema_version=_require_int(raw, "schema_version"),
        corpus_version=_require_text(raw, "corpus_version"),
        thresholds=thresholds,
        cases=cases,
    )


def _parse_thresholds(value: dict[str, Any]) -> BenchmarkThresholds:
    return BenchmarkThresholds(
        minimum_pass_rate=_require_number(value, "minimum_pass_rate"),
        maximum_false_completions=_require_int(value, "maximum_false_completions"),
        maximum_duplicate_side_effects=_require_int(
            value, "maximum_duplicate_side_effects"
        ),
        maximum_abandoned_runs=_require_int(value, "maximum_abandoned_runs"),
        maximum_suite_duration_ms=_require_number(value, "maximum_suite_duration_ms"),
    )


def _parse_case(value: Any) -> BenchmarkCase:
    if not isinstance(value, dict):
        raise ValueError("benchmark case must be an object")
    expected = _require_object(value, "expected")
    metrics = expected.get("metrics", {})
    if not isinstance(metrics, dict):
        raise ValueError("benchmark expected.metrics must be an object")
    normalized_metrics: dict[str, MetricValue] = {}
    for key, metric in metrics.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError("benchmark metric names must be non-empty strings")
        if not isinstance(metric, (bool, int, float, str)):
            raise ValueError(f"benchmark metric must be scalar: {key}")
        normalized_metrics[key] = metric
    try:
        suite = BenchmarkSuite(_require_text(value, "suite"))
        kind = BenchmarkKind(_require_text(value, "kind"))
        status = TaskRunStatus(_require_text(expected, "status"))
    except ValueError as exc:
        raise ValueError(f"invalid benchmark enum value: {exc}") from exc
    return BenchmarkCase(
        id=_require_text(value, "id"),
        name=_require_text(value, "name"),
        suite=suite,
        kind=kind,
        max_duration_ms=_require_number(value, "max_duration_ms"),
        expected=BenchmarkExpectation(
            accepted=_require_bool(expected, "accepted"),
            status=status,
            recovered=_require_bool(expected, "recovered"),
            metrics=normalized_metrics,
        ),
    )


def _require_object(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    return value


def _require_text(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def _require_bool(data: dict[str, Any], key: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a boolean")
    return value


def _require_int(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    return value


def _require_number(data: dict[str, Any], key: str) -> float:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} must be a number")
    return float(value)
