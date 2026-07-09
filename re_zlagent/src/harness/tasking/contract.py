"""Task contracts and run state for deterministic task execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")


class CriterionType(str, Enum):
    """Supported acceptance criterion families."""

    PLAN_QUALITY = "plan_quality"
    TOOL_EVIDENCE = "tool_evidence"
    STATE_CHANGE = "state_change"
    TEST_RESULT = "test_result"
    HUMAN_APPROVAL = "human_approval"
    FRESHNESS = "freshness"
    NO_REGRESSION = "no_regression"


class CriterionStatus(str, Enum):
    """Status for a single acceptance criterion."""

    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"


class TaskRunStatus(str, Enum):
    """Top-level run status used by tasking and checkpoint layers."""

    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    WAITING_USER = "waiting_user"
    RECOVERING = "recovering"
    ACCEPTANCE_FAILED = "acceptance_failed"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class AcceptanceCriterion:
    """A deterministic condition required before a task can be completed."""

    id: str
    description: str
    type: CriterionType
    required: bool = True
    evidence_refs: tuple[str, ...] = field(default_factory=tuple)
    freshness_window_seconds: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text(self.id, "criterion.id")
        _require_text(self.description, "criterion.description")
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        object.__setattr__(self, "metadata", dict(self.metadata))
        if self.freshness_window_seconds is not None and self.freshness_window_seconds <= 0:
            raise ValueError("freshness_window_seconds must be positive")


@dataclass(frozen=True, slots=True)
class TaskContract:
    """Stable task completion contract for a run."""

    id: str
    user_goal: str
    stakeholders: tuple[str, ...] = field(default_factory=tuple)
    mvp_scope: tuple[str, ...] = field(default_factory=tuple)
    out_of_scope: tuple[str, ...] = field(default_factory=tuple)
    acceptance_criteria: tuple[AcceptanceCriterion, ...] = field(default_factory=tuple)
    capability_boundaries: dict[str, Any] = field(default_factory=dict)
    freshness_policy: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=_utc_now)
    version: str = "1"

    def __post_init__(self) -> None:
        _require_text(self.id, "contract.id")
        _require_text(self.user_goal, "contract.user_goal")
        _require_text(self.version, "contract.version")
        object.__setattr__(self, "stakeholders", tuple(self.stakeholders))
        object.__setattr__(self, "mvp_scope", tuple(self.mvp_scope))
        object.__setattr__(self, "out_of_scope", tuple(self.out_of_scope))
        object.__setattr__(self, "acceptance_criteria", tuple(self.acceptance_criteria))
        object.__setattr__(self, "capability_boundaries", dict(self.capability_boundaries))
        object.__setattr__(self, "freshness_policy", dict(self.freshness_policy))
        if not self.acceptance_criteria:
            raise ValueError("contract.acceptance_criteria must not be empty")
        seen: set[str] = set()
        for criterion in self.acceptance_criteria:
            if criterion.id in seen:
                raise ValueError(f"duplicate acceptance criterion id: {criterion.id}")
            seen.add(criterion.id)

    def required_criteria(self) -> tuple[AcceptanceCriterion, ...]:
        return tuple(item for item in self.acceptance_criteria if item.required)


@dataclass(frozen=True, slots=True)
class TaskRun:
    """Runtime projection for one execution of a task contract."""

    id: str
    contract_id: str
    status: TaskRunStatus = TaskRunStatus.CREATED
    current_checkpoint_id: str | None = None
    event_seq: int = 0
    model_name: str | None = None
    prompt_version: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text(self.id, "run.id")
        _require_text(self.contract_id, "run.contract_id")
        if self.event_seq < 0:
            raise ValueError("run.event_seq must be >= 0")
        object.__setattr__(self, "metadata", dict(self.metadata))

    def with_status(
        self,
        status: TaskRunStatus,
        *,
        checkpoint_id: str | None = None,
        event_seq: int | None = None,
    ) -> "TaskRun":
        return TaskRun(
            id=self.id,
            contract_id=self.contract_id,
            status=status,
            current_checkpoint_id=checkpoint_id
            if checkpoint_id is not None
            else self.current_checkpoint_id,
            event_seq=self.event_seq if event_seq is None else event_seq,
            model_name=self.model_name,
            prompt_version=self.prompt_version,
            metadata=self.metadata,
        )
