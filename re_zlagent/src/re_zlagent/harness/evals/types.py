"""Benchmark and evaluation result types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from re_zlagent.harness.agent import AgentRunRequest
from re_zlagent.harness.tasking import CheckpointStatus, TaskEventType, TaskRunStatus


@dataclass(frozen=True, slots=True)
class EvalScenario:
    """One benchmark case for an agent run.

    Scenarios express expectations about runtime output. They do not define
    completion rules; completion still belongs to the runtime acceptance gate.
    """

    id: str
    name: str
    request: AgentRunRequest
    expected_accepted: bool | None = None
    expected_status: TaskRunStatus | None = None
    required_event_types: tuple[TaskEventType, ...] = field(default_factory=tuple)
    required_checkpoint_statuses: tuple[CheckpointStatus, ...] = field(default_factory=tuple)
    required_evidence_refs: tuple[str, ...] = field(default_factory=tuple)
    max_tool_failures: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("scenario.id must be non-empty")
        if not self.name.strip():
            raise ValueError("scenario.name must be non-empty")
        object.__setattr__(self, "required_event_types", tuple(self.required_event_types))
        object.__setattr__(
            self,
            "required_checkpoint_statuses",
            tuple(self.required_checkpoint_statuses),
        )
        object.__setattr__(self, "required_evidence_refs", tuple(self.required_evidence_refs))
        object.__setattr__(self, "metadata", dict(self.metadata))
        has_expectation = any((
            self.expected_accepted is not None,
            self.expected_status is not None,
            bool(self.required_event_types),
            bool(self.required_checkpoint_statuses),
            bool(self.required_evidence_refs),
            self.max_tool_failures is not None,
        ))
        if not has_expectation:
            raise ValueError("scenario must define at least one expectation")
        if self.max_tool_failures is not None and self.max_tool_failures < 0:
            raise ValueError("max_tool_failures must be >= 0")


@dataclass(frozen=True, slots=True)
class EvalCaseResult:
    """Result of evaluating one scenario."""

    scenario_id: str
    scenario_name: str
    passed: bool
    score: float
    failures: tuple[str, ...] = field(default_factory=tuple)
    run_id: str = ""
    run_status: TaskRunStatus | None = None
    accepted: bool = False
    event_count: int = 0
    checkpoint_count: int = 0
    tool_failure_count: int = 0
    evidence_refs: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "failures", tuple(self.failures))
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        if self.score < 0 or self.score > 1:
            raise ValueError("score must be between 0 and 1")

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "scenario_name": self.scenario_name,
            "passed": self.passed,
            "score": self.score,
            "failures": list(self.failures),
            "run_id": self.run_id,
            "run_status": self.run_status.value if self.run_status else None,
            "accepted": self.accepted,
            "event_count": self.event_count,
            "checkpoint_count": self.checkpoint_count,
            "tool_failure_count": self.tool_failure_count,
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass(frozen=True, slots=True)
class EvalSuiteResult:
    """Aggregate result for a benchmark suite."""

    results: tuple[EvalCaseResult, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "results", tuple(self.results))

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed_count(self) -> int:
        return sum(1 for result in self.results if result.passed)

    @property
    def failed_count(self) -> int:
        return self.total - self.passed_count

    @property
    def pass_rate(self) -> float:
        if not self.results:
            return 0.0
        return self.passed_count / self.total

    @property
    def average_score(self) -> float:
        if not self.results:
            return 0.0
        return sum(result.score for result in self.results) / self.total

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "passed_count": self.passed_count,
            "failed_count": self.failed_count,
            "pass_rate": self.pass_rate,
            "average_score": self.average_score,
            "results": [result.to_dict() for result in self.results],
        }
