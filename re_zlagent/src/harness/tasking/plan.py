"""Plan-step primitives for stage completion and static DAG checks."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")


class StepStatus(str, Enum):
    """Status for one planned stage in a run."""

    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class PlanStep:
    """Planned stage metadata used for local step verification.

    DAG dependencies are static metadata. Runtime execution stays linear until
    a later scheduler stage.
    """

    id: str
    title: str
    expected_output: str = ""
    verification: str = ""
    depends_on: tuple[str, ...] = field(default_factory=tuple)
    required_evidence_refs: tuple[str, ...] = field(default_factory=tuple)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text(self.id, "step.id")
        _require_text(self.title, "step.title")
        object.__setattr__(
            self,
            "depends_on",
            tuple(str(item) for item in self.depends_on),
        )
        object.__setattr__(
            self,
            "required_evidence_refs",
            tuple(str(item) for item in self.required_evidence_refs),
        )
        object.__setattr__(self, "metadata", dict(self.metadata))
        if self.id in self.depends_on:
            raise ValueError("step cannot depend on itself")


@dataclass(frozen=True, slots=True)
class PlanDAG:
    """Static dependency graph for plan steps.

    This validates dependency shape and order only. It does not schedule or
    parallelize execution.
    """

    steps: tuple[PlanStep, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "steps", tuple(self.steps))
        seen: set[str] = set()
        for step in self.steps:
            if step.id in seen:
                raise ValueError(f"duplicate plan step id: {step.id}")
            seen.add(step.id)
        for step in self.steps:
            missing = sorted(set(step.depends_on).difference(seen))
            if missing:
                raise ValueError(
                    f"step {step.id} depends on unknown steps: {', '.join(missing)}"
                )
        self._topological_ids()

    def topological_step_ids(self) -> tuple[str, ...]:
        """Return a deterministic topological order."""

        return self._topological_ids()

    def validate_linear_order(self, step_ids: tuple[str, ...] | list[str]) -> None:
        """Ensure a linear execution order respects declared dependencies."""

        known = {step.id for step in self.steps}
        seen: set[str] = set()
        for step_id in step_ids:
            if step_id not in known:
                raise ValueError(f"unknown step in execution order: {step_id}")
            step = self.step_by_id(step_id)
            missing = tuple(dep for dep in step.depends_on if dep not in seen)
            if missing:
                raise ValueError(
                    f"step {step_id} is scheduled before dependencies: "
                    f"{', '.join(missing)}"
                )
            seen.add(step_id)

    def step_by_id(self, step_id: str) -> PlanStep:
        for step in self.steps:
            if step.id == step_id:
                return step
        raise ValueError(f"unknown plan step id: {step_id}")

    def _topological_ids(self) -> tuple[str, ...]:
        remaining = {step.id: set(step.depends_on) for step in self.steps}
        ordered: list[str] = []
        while remaining:
            ready = sorted(
                step_id
                for step_id, deps in remaining.items()
                if deps.issubset(ordered)
            )
            if not ready:
                raise ValueError(
                    f"plan DAG contains a cycle: {', '.join(sorted(remaining))}"
                )
            for step_id in ready:
                ordered.append(step_id)
                del remaining[step_id]
        return tuple(ordered)


@dataclass(frozen=True, slots=True)
class StepVerification:
    """Deterministic local verdict for one plan step."""

    step_id: str
    status: StepStatus
    reason: str = ""
    evidence_refs: tuple[str, ...] = field(default_factory=tuple)
    missing_evidence_refs: tuple[str, ...] = field(default_factory=tuple)
    checkpoint_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text(self.step_id, "verification.step_id")
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        object.__setattr__(
            self,
            "missing_evidence_refs",
            tuple(self.missing_evidence_refs),
        )
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def passed(self) -> bool:
        return self.status is StepStatus.PASSED

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "status": self.status.value,
            "reason": self.reason,
            "evidence_refs": list(self.evidence_refs),
            "missing_evidence_refs": list(self.missing_evidence_refs),
            "checkpoint_id": self.checkpoint_id,
            "metadata": dict(self.metadata),
        }


class StepVerifier:
    """Rule-based verifier for one linear plan step."""

    def verify(
        self,
        step: PlanStep,
        *,
        tool_ok: bool,
        evidence_refs: tuple[str, ...] | list[str] | set[str] = (),
        blocked: bool = False,
        failure_reason: str | None = None,
        checkpoint_id: str | None = None,
    ) -> StepVerification:
        observed = tuple(sorted(str(item) for item in evidence_refs))
        if blocked:
            return StepVerification(
                step_id=step.id,
                status=StepStatus.BLOCKED,
                reason=failure_reason or "step is blocked",
                evidence_refs=observed,
                checkpoint_id=checkpoint_id,
            )
        if not tool_ok:
            return StepVerification(
                step_id=step.id,
                status=StepStatus.FAILED,
                reason=failure_reason or "step tool execution failed",
                evidence_refs=observed,
                checkpoint_id=checkpoint_id,
            )

        missing = tuple(
            sorted(set(step.required_evidence_refs).difference(observed))
        )
        if missing:
            return StepVerification(
                step_id=step.id,
                status=StepStatus.FAILED,
                reason="required step evidence missing",
                evidence_refs=observed,
                missing_evidence_refs=missing,
                checkpoint_id=checkpoint_id,
            )

        return StepVerification(
            step_id=step.id,
            status=StepStatus.PASSED,
            reason="step verification passed",
            evidence_refs=observed,
            checkpoint_id=checkpoint_id,
        )
