"""Conservative DAG execution-readiness assessment."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .long_task import LongTaskProjection, ProgramPlan
from .plan import PlanStep


@dataclass(frozen=True, slots=True)
class DagExecutionAssessment:
    """Read-only DAG scheduling guidance.

    This is not a scheduler. It only says what a future scheduler may safely
    consider from the current frontier.
    """

    frontier_step_ids: tuple[str, ...] = field(default_factory=tuple)
    linear_next_step_id: str | None = None
    parallel_candidate_step_ids: tuple[str, ...] = field(default_factory=tuple)
    linear_only_step_ids: tuple[str, ...] = field(default_factory=tuple)
    unsafe_reasons: dict[str, tuple[str, ...]] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "frontier_step_ids", tuple(self.frontier_step_ids))
        object.__setattr__(
            self,
            "parallel_candidate_step_ids",
            tuple(self.parallel_candidate_step_ids),
        )
        object.__setattr__(self, "linear_only_step_ids", tuple(self.linear_only_step_ids))
        object.__setattr__(
            self,
            "unsafe_reasons",
            {key: tuple(value) for key, value in self.unsafe_reasons.items()},
        )
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def can_parallelize(self) -> bool:
        return len(self.parallel_candidate_step_ids) > 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "frontier_step_ids": list(self.frontier_step_ids),
            "linear_next_step_id": self.linear_next_step_id,
            "parallel_candidate_step_ids": list(self.parallel_candidate_step_ids),
            "linear_only_step_ids": list(self.linear_only_step_ids),
            "can_parallelize": self.can_parallelize,
            "unsafe_reasons": {
                key: list(value)
                for key, value in self.unsafe_reasons.items()
            },
            "metadata": dict(self.metadata),
        }


class DagExecutionPolicy:
    """Conservative policy for future DAG schedulers."""

    def assess(
        self,
        *,
        program_plan: ProgramPlan,
        projection: LongTaskProjection,
    ) -> DagExecutionAssessment:
        frontier = projection.frontier_step_ids
        parallel: list[str] = []
        linear_only: list[str] = []
        unsafe: dict[str, tuple[str, ...]] = {}
        claimed_targets: set[str] = set()

        for step_id in frontier:
            step = program_plan.step_by_id(step_id)
            reasons = self._unsafe_reasons(step, claimed_targets)
            if reasons:
                linear_only.append(step_id)
                unsafe[step_id] = reasons
                continue
            parallel.append(step_id)
            claimed_targets.update(self._write_targets(step))

        return DagExecutionAssessment(
            frontier_step_ids=frontier,
            linear_next_step_id=frontier[0] if frontier else None,
            parallel_candidate_step_ids=tuple(parallel),
            linear_only_step_ids=tuple(linear_only),
            unsafe_reasons=unsafe,
            metadata={
                "policy": "conservative",
                "parallel_requires_read_only_or_explicit_parallel_safe": True,
            },
        )

    def _unsafe_reasons(
        self,
        step: PlanStep,
        claimed_targets: set[str],
    ) -> tuple[str, ...]:
        metadata = dict(step.metadata)
        reasons: list[str] = []

        if bool(metadata.get("allow_confirm")):
            reasons.append("confirm-tier step")
        if bool(metadata.get("requires_user")):
            reasons.append("requires user interaction")
        if bool(metadata.get("external_side_effect")):
            reasons.append("external side effect")

        write_targets = self._write_targets(step)
        conflicts = sorted(write_targets.intersection(claimed_targets))
        if conflicts:
            reasons.append(f"write target conflict: {', '.join(conflicts)}")

        read_only = bool(metadata.get("read_only") or metadata.get("is_read_only"))
        explicitly_safe = bool(metadata.get("parallel_safe"))
        if not read_only and not explicitly_safe:
            reasons.append("not marked read-only or parallel-safe")

        return tuple(reasons)

    def _write_targets(self, step: PlanStep) -> set[str]:
        values = step.metadata.get("write_targets") or ()
        if isinstance(values, str):
            return {values}
        return {str(item) for item in values}
