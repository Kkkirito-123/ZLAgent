"""Long-task planning and replay projections.

These types keep long-running work recoverable without relying on chat history.
They are intentionally read-only helpers around TaskStore events, checkpoints,
and explicit pending interactions.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from .checkpoint import Checkpoint
from .events import TaskEvent, TaskEventType
from .plan import PlanDAG, PlanStep, StepStatus


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")


class PhaseStatus(str, Enum):
    """Status for a group of DAG steps."""

    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"


class InteractionStatus(str, Enum):
    """Lifecycle state for a user-facing wait point."""

    OPEN = "open"
    RESOLVED = "resolved"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class InteractionKind(str, Enum):
    """Reason the runtime is waiting for the user/operator."""

    USER_INPUT = "user_input"
    USER_APPROVAL = "user_approval"
    CLARIFICATION = "clarification"
    MANUAL_REVIEW = "manual_review"


class ArtifactKind(str, Enum):
    """Long-task artifact categories."""

    FILE = "file"
    REPORT = "report"
    EVIDENCE = "evidence"
    OUTPUT = "output"
    SNAPSHOT = "snapshot"


class SideEffectStatus(str, Enum):
    """Idempotent side-effect ledger status."""

    PLANNED = "planned"
    DISPATCHING = "dispatching"
    APPLIED = "applied"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    REVERTED = "reverted"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class ProgramPhase:
    """A human-meaningful phase over one or more DAG steps."""

    id: str
    title: str
    step_ids: tuple[str, ...] = field(default_factory=tuple)
    acceptance_refs: tuple[str, ...] = field(default_factory=tuple)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text(self.id, "phase.id")
        _require_text(self.title, "phase.title")
        object.__setattr__(self, "step_ids", tuple(str(item) for item in self.step_ids))
        object.__setattr__(
            self,
            "acceptance_refs",
            tuple(str(item) for item in self.acceptance_refs),
        )
        object.__setattr__(self, "metadata", dict(self.metadata))
        if not self.step_ids:
            raise ValueError("phase.step_ids must not be empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "step_ids": list(self.step_ids),
            "acceptance_refs": list(self.acceptance_refs),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ProgramPlan:
    """Long-task plan that binds a contract to a static DAG."""

    id: str
    contract_id: str
    dag: PlanDAG
    phases: tuple[ProgramPhase, ...] = field(default_factory=tuple)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text(self.id, "program_plan.id")
        _require_text(self.contract_id, "program_plan.contract_id")
        object.__setattr__(self, "metadata", dict(self.metadata))
        phases = tuple(self.phases)
        if not phases and self.dag.steps:
            phases = (
                ProgramPhase(
                    id="main",
                    title="Main",
                    step_ids=self.dag.topological_step_ids(),
                ),
            )
        object.__setattr__(self, "phases", phases)
        self._validate_phases()

    def _validate_phases(self) -> None:
        known_steps = {step.id for step in self.dag.steps}
        seen_phases: set[str] = set()
        seen_steps: set[str] = set()
        for phase in self.phases:
            if phase.id in seen_phases:
                raise ValueError(f"duplicate phase id: {phase.id}")
            seen_phases.add(phase.id)
            missing = sorted(set(phase.step_ids).difference(known_steps))
            if missing:
                raise ValueError(
                    f"phase {phase.id} references unknown steps: {', '.join(missing)}"
                )
            duplicate_steps = sorted(set(phase.step_ids).intersection(seen_steps))
            if duplicate_steps:
                raise ValueError(
                    f"phase {phase.id} repeats steps: {', '.join(duplicate_steps)}"
                )
            seen_steps.update(phase.step_ids)
        if known_steps and seen_steps != known_steps:
            missing = sorted(known_steps.difference(seen_steps))
            raise ValueError(f"program phases do not cover steps: {', '.join(missing)}")

    def step_by_id(self, step_id: str) -> PlanStep:
        return self.dag.step_by_id(step_id)

    def phase_for_step(self, step_id: str) -> ProgramPhase | None:
        for phase in self.phases:
            if step_id in phase.step_ids:
                return phase
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "contract_id": self.contract_id,
            "steps": [
                {
                    "id": step.id,
                    "title": step.title,
                    "expected_output": step.expected_output,
                    "verification": step.verification,
                    "depends_on": list(step.depends_on),
                    "required_evidence_refs": list(step.required_evidence_refs),
                    "metadata": dict(step.metadata),
                }
                for step in self.dag.steps
            ],
            "phases": [phase.to_dict() for phase in self.phases],
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class PendingInteraction:
    """Durable wait point for user/operator input."""

    id: str
    run_id: str
    checkpoint_id: str
    question: str
    required_by_step_id: str | None = None
    kind: InteractionKind = InteractionKind.USER_INPUT
    status: InteractionStatus = InteractionStatus.OPEN
    resume_token: str = field(default_factory=lambda: f"resume_{uuid4().hex}")
    answer: str | None = None
    created_at: datetime = field(default_factory=_utc_now)
    expires_at: datetime | None = None
    resolved_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text(self.id, "pending_interaction.id")
        _require_text(self.run_id, "pending_interaction.run_id")
        _require_text(self.checkpoint_id, "pending_interaction.checkpoint_id")
        _require_text(self.question, "pending_interaction.question")
        _require_text(self.resume_token, "pending_interaction.resume_token")
        if self.required_by_step_id is not None:
            _require_text(
                self.required_by_step_id,
                "pending_interaction.required_by_step_id",
            )
        object.__setattr__(self, "metadata", dict(self.metadata))
        if self.status is InteractionStatus.RESOLVED and not self.answer:
            raise ValueError("resolved pending interaction must include answer")

    @property
    def open(self) -> bool:
        return self.status is InteractionStatus.OPEN

    def resolve(
        self,
        answer: str,
        *,
        resolved_at: datetime | None = None,
    ) -> "PendingInteraction":
        _require_text(answer, "pending_interaction.answer")
        return replace(
            self,
            status=InteractionStatus.RESOLVED,
            answer=answer,
            resolved_at=resolved_at or _utc_now(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "run_id": self.run_id,
            "checkpoint_id": self.checkpoint_id,
            "question": self.question,
            "required_by_step_id": self.required_by_step_id,
            "kind": self.kind.value,
            "status": self.status.value,
            "resume_token": self.resume_token,
            "answer": self.answer,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    """Pointer to an output or evidence artifact created during a run."""

    id: str
    run_id: str
    ref: str
    kind: ArtifactKind
    producer_step_id: str | None = None
    evidence_refs: tuple[str, ...] = field(default_factory=tuple)
    created_at: datetime = field(default_factory=_utc_now)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text(self.id, "artifact.id")
        _require_text(self.run_id, "artifact.run_id")
        _require_text(self.ref, "artifact.ref")
        if self.producer_step_id is not None:
            _require_text(self.producer_step_id, "artifact.producer_step_id")
        object.__setattr__(
            self,
            "evidence_refs",
            tuple(str(item) for item in self.evidence_refs),
        )
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "run_id": self.run_id,
            "ref": self.ref,
            "kind": self.kind.value,
            "producer_step_id": self.producer_step_id,
            "evidence_refs": list(self.evidence_refs),
            "created_at": self.created_at.isoformat(),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class SideEffectRecord:
    """Durable outbox entry for an externally visible action."""

    id: str
    run_id: str
    idempotency_key: str
    type: str
    target: str
    status: SideEffectStatus = SideEffectStatus.PLANNED
    intent_fingerprint: str = ""
    producer_step_id: str | None = None
    attempt_count: int = 0
    created_at: datetime = field(default_factory=_utc_now)
    updated_at: datetime = field(default_factory=_utc_now)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text(self.id, "side_effect.id")
        _require_text(self.run_id, "side_effect.run_id")
        _require_text(self.idempotency_key, "side_effect.idempotency_key")
        _require_text(self.type, "side_effect.type")
        _require_text(self.target, "side_effect.target")
        if self.producer_step_id is not None:
            _require_text(self.producer_step_id, "side_effect.producer_step_id")
        if self.intent_fingerprint and not self.intent_fingerprint.strip():
            raise ValueError("side_effect.intent_fingerprint must be non-empty")
        if self.attempt_count < 0:
            raise ValueError("side_effect.attempt_count must be >= 0")
        object.__setattr__(self, "metadata", dict(self.metadata))

    def transition(
        self,
        status: SideEffectStatus,
        *,
        metadata: dict[str, Any] | None = None,
        updated_at: datetime | None = None,
    ) -> "SideEffectRecord":
        """Apply one legal outbox state transition."""

        allowed = {
            SideEffectStatus.PLANNED: {
                SideEffectStatus.DISPATCHING,
                SideEffectStatus.REVERTED,
            },
            SideEffectStatus.DISPATCHING: {
                SideEffectStatus.PLANNED,
                SideEffectStatus.APPLIED,
                SideEffectStatus.FAILED,
                SideEffectStatus.UNCERTAIN,
            },
            SideEffectStatus.APPLIED: {
                SideEffectStatus.CONFIRMED,
                SideEffectStatus.REVERTED,
                SideEffectStatus.UNCERTAIN,
            },
            SideEffectStatus.FAILED: {
                SideEffectStatus.PLANNED,
                SideEffectStatus.REVERTED,
            },
            SideEffectStatus.UNCERTAIN: {
                SideEffectStatus.CONFIRMED,
                SideEffectStatus.FAILED,
                SideEffectStatus.REVERTED,
            },
            SideEffectStatus.CONFIRMED: set(),
            SideEffectStatus.REVERTED: set(),
        }
        if status not in allowed[self.status]:
            raise ValueError(
                f"invalid side-effect transition: {self.status.value} -> {status.value}"
            )
        merged_metadata = dict(self.metadata)
        merged_metadata.update(metadata or {})
        return replace(
            self,
            status=status,
            attempt_count=(
                self.attempt_count + 1
                if status is SideEffectStatus.DISPATCHING
                else self.attempt_count
            ),
            updated_at=updated_at or _utc_now(),
            metadata=merged_metadata,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "run_id": self.run_id,
            "idempotency_key": self.idempotency_key,
            "type": self.type,
            "target": self.target,
            "status": self.status.value,
            "intent_fingerprint": self.intent_fingerprint,
            "producer_step_id": self.producer_step_id,
            "attempt_count": self.attempt_count,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class StepRun:
    """Projected state for one DAG step."""

    id: str
    status: StepStatus = StepStatus.PENDING
    checkpoint_id: str | None = None
    evidence_refs: tuple[str, ...] = field(default_factory=tuple)
    missing_evidence_refs: tuple[str, ...] = field(default_factory=tuple)
    started_event_id: str | None = None
    verified_event_id: str | None = None
    blocked_by: tuple[str, ...] = field(default_factory=tuple)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text(self.id, "step_run.id")
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        object.__setattr__(
            self,
            "missing_evidence_refs",
            tuple(self.missing_evidence_refs),
        )
        object.__setattr__(self, "blocked_by", tuple(self.blocked_by))
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def passed(self) -> bool:
        return self.status is StepStatus.PASSED

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status.value,
            "checkpoint_id": self.checkpoint_id,
            "evidence_refs": list(self.evidence_refs),
            "missing_evidence_refs": list(self.missing_evidence_refs),
            "started_event_id": self.started_event_id,
            "verified_event_id": self.verified_event_id,
            "blocked_by": list(self.blocked_by),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class PhaseRun:
    """Projected state for one program phase."""

    id: str
    title: str
    status: PhaseStatus
    step_ids: tuple[str, ...] = field(default_factory=tuple)
    completed_step_ids: tuple[str, ...] = field(default_factory=tuple)
    blocked_step_ids: tuple[str, ...] = field(default_factory=tuple)
    failed_step_ids: tuple[str, ...] = field(default_factory=tuple)
    running_step_ids: tuple[str, ...] = field(default_factory=tuple)
    pending_interaction_ids: tuple[str, ...] = field(default_factory=tuple)
    checkpoint_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text(self.id, "phase_run.id")
        _require_text(self.title, "phase_run.title")
        object.__setattr__(self, "step_ids", tuple(self.step_ids))
        object.__setattr__(self, "completed_step_ids", tuple(self.completed_step_ids))
        object.__setattr__(self, "blocked_step_ids", tuple(self.blocked_step_ids))
        object.__setattr__(self, "failed_step_ids", tuple(self.failed_step_ids))
        object.__setattr__(self, "running_step_ids", tuple(self.running_step_ids))
        object.__setattr__(
            self,
            "pending_interaction_ids",
            tuple(self.pending_interaction_ids),
        )
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status.value,
            "step_ids": list(self.step_ids),
            "completed_step_ids": list(self.completed_step_ids),
            "blocked_step_ids": list(self.blocked_step_ids),
            "failed_step_ids": list(self.failed_step_ids),
            "running_step_ids": list(self.running_step_ids),
            "pending_interaction_ids": list(self.pending_interaction_ids),
            "checkpoint_id": self.checkpoint_id,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class LongTaskProjection:
    """Replayable read model for one long task run."""

    run_id: str
    program_plan_id: str
    event_seq: int
    step_runs: dict[str, StepRun] = field(default_factory=dict)
    phase_runs: dict[str, PhaseRun] = field(default_factory=dict)
    frontier_step_ids: tuple[str, ...] = field(default_factory=tuple)
    completed_step_ids: tuple[str, ...] = field(default_factory=tuple)
    failed_step_ids: tuple[str, ...] = field(default_factory=tuple)
    blocked_step_ids: tuple[str, ...] = field(default_factory=tuple)
    pending_interaction_ids: tuple[str, ...] = field(default_factory=tuple)
    latest_checkpoint_id: str | None = None
    latest_checkpoint_status: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text(self.run_id, "projection.run_id")
        _require_text(self.program_plan_id, "projection.program_plan_id")
        if self.event_seq < 0:
            raise ValueError("projection.event_seq must be >= 0")
        object.__setattr__(self, "step_runs", dict(self.step_runs))
        object.__setattr__(self, "phase_runs", dict(self.phase_runs))
        object.__setattr__(self, "frontier_step_ids", tuple(self.frontier_step_ids))
        object.__setattr__(self, "completed_step_ids", tuple(self.completed_step_ids))
        object.__setattr__(self, "failed_step_ids", tuple(self.failed_step_ids))
        object.__setattr__(self, "blocked_step_ids", tuple(self.blocked_step_ids))
        object.__setattr__(
            self,
            "pending_interaction_ids",
            tuple(self.pending_interaction_ids),
        )
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "program_plan_id": self.program_plan_id,
            "event_seq": self.event_seq,
            "step_runs": {
                step_id: step.to_dict()
                for step_id, step in self.step_runs.items()
            },
            "phase_runs": {
                phase_id: phase.to_dict()
                for phase_id, phase in self.phase_runs.items()
            },
            "frontier_step_ids": list(self.frontier_step_ids),
            "completed_step_ids": list(self.completed_step_ids),
            "failed_step_ids": list(self.failed_step_ids),
            "blocked_step_ids": list(self.blocked_step_ids),
            "pending_interaction_ids": list(self.pending_interaction_ids),
            "latest_checkpoint_id": self.latest_checkpoint_id,
            "latest_checkpoint_status": self.latest_checkpoint_status,
            "metadata": dict(self.metadata),
        }


class LongTaskProjector:
    """Build long-task projections from append-only runtime facts."""

    def project(
        self,
        *,
        run_id: str,
        program_plan: ProgramPlan,
        events: tuple[TaskEvent, ...] | list[TaskEvent] = (),
        checkpoints: tuple[Checkpoint, ...] | list[Checkpoint] = (),
        pending_interactions: tuple[PendingInteraction, ...] | list[PendingInteraction] = (),
    ) -> LongTaskProjection:
        _require_text(run_id, "projection.run_id")
        ordered_events = tuple(sorted(events, key=lambda item: item.seq))
        step_runs = {
            step.id: StepRun(id=step.id)
            for step in program_plan.dag.steps
        }

        for event in ordered_events:
            self._apply_event(step_runs, event)

        open_interactions = tuple(
            item for item in pending_interactions
            if item.run_id == run_id and item.open
        )
        step_runs = self._apply_blocks(
            program_plan=program_plan,
            step_runs=step_runs,
            open_interactions=open_interactions,
        )

        completed = tuple(
            step_id
            for step_id in program_plan.dag.topological_step_ids()
            if step_runs[step_id].status is StepStatus.PASSED
        )
        failed = tuple(
            step_id
            for step_id in program_plan.dag.topological_step_ids()
            if step_runs[step_id].status is StepStatus.FAILED
        )
        blocked = tuple(
            step_id
            for step_id in program_plan.dag.topological_step_ids()
            if self._has_intervention_block(step_runs[step_id])
        )
        frontier = self._frontier(program_plan, step_runs)
        latest_checkpoint = tuple(checkpoints)[-1] if checkpoints else None
        phase_runs = self._phase_runs(
            program_plan=program_plan,
            step_runs=step_runs,
            open_interactions=open_interactions,
            latest_checkpoint=latest_checkpoint,
        )

        return LongTaskProjection(
            run_id=run_id,
            program_plan_id=program_plan.id,
            event_seq=ordered_events[-1].seq if ordered_events else 0,
            step_runs=step_runs,
            phase_runs=phase_runs,
            frontier_step_ids=frontier,
            completed_step_ids=completed,
            failed_step_ids=failed,
            blocked_step_ids=blocked,
            pending_interaction_ids=tuple(item.id for item in open_interactions),
            latest_checkpoint_id=latest_checkpoint.id if latest_checkpoint else None,
            latest_checkpoint_status=(
                latest_checkpoint.status.value if latest_checkpoint else None
            ),
        )

    def _apply_event(self, step_runs: dict[str, StepRun], event: TaskEvent) -> None:
        if event.type is TaskEventType.PLAN_STEP_STARTED:
            step_id = event.payload.get("step_id")
            if isinstance(step_id, str) and step_id in step_runs:
                step_runs[step_id] = replace(
                    step_runs[step_id],
                    status=StepStatus.RUNNING,
                    started_event_id=event.id,
                )
            return

        if event.type is TaskEventType.PLAN_STEP_VERIFIED:
            step_id = event.payload.get("step_id")
            status = event.payload.get("status")
            if not isinstance(step_id, str) or step_id not in step_runs:
                return
            if not isinstance(status, str):
                return
            try:
                parsed_status = StepStatus(status)
            except ValueError:
                return
            step_runs[step_id] = replace(
                step_runs[step_id],
                status=parsed_status,
                checkpoint_id=event.payload.get("checkpoint_id"),
                evidence_refs=tuple(
                    str(item) for item in event.payload.get("evidence_refs") or ()
                ),
                missing_evidence_refs=tuple(
                    str(item)
                    for item in event.payload.get("missing_evidence_refs") or ()
                ),
                verified_event_id=event.id,
            )

    def _apply_blocks(
        self,
        *,
        program_plan: ProgramPlan,
        step_runs: dict[str, StepRun],
        open_interactions: tuple[PendingInteraction, ...],
    ) -> dict[str, StepRun]:
        updated = dict(step_runs)
        interactions_by_step: dict[str, list[str]] = {}
        for interaction in open_interactions:
            if interaction.required_by_step_id:
                interactions_by_step.setdefault(
                    interaction.required_by_step_id,
                    [],
                ).append(f"interaction:{interaction.id}")

        passed = {
            step_id for step_id, run in updated.items()
            if run.status is StepStatus.PASSED
        }
        for step in program_plan.dag.steps:
            run = updated[step.id]
            dependency_blocks = [
                f"dependency:{dep}"
                for dep in step.depends_on
                if dep not in passed
            ]
            interaction_blocks = interactions_by_step.get(step.id, ())
            blocked_by = [*dependency_blocks, *interaction_blocks]
            if blocked_by:
                status = (
                    StepStatus.BLOCKED
                    if (
                        interaction_blocks
                        and run.status in {StepStatus.PENDING, StepStatus.RUNNING}
                    )
                    else run.status
                )
                updated[step.id] = replace(
                    run,
                    status=status,
                    blocked_by=tuple(blocked_by),
                )
        return updated

    def _frontier(
        self,
        program_plan: ProgramPlan,
        step_runs: dict[str, StepRun],
    ) -> tuple[str, ...]:
        ready: list[str] = []
        for step_id in program_plan.dag.topological_step_ids():
            run = step_runs[step_id]
            if run.status is not StepStatus.PENDING:
                continue
            step = program_plan.dag.step_by_id(step_id)
            if all(step_runs[dep].status is StepStatus.PASSED for dep in step.depends_on):
                ready.append(step_id)
        return tuple(ready)

    def _phase_runs(
        self,
        *,
        program_plan: ProgramPlan,
        step_runs: dict[str, StepRun],
        open_interactions: tuple[PendingInteraction, ...],
        latest_checkpoint: Checkpoint | None,
    ) -> dict[str, PhaseRun]:
        interactions_by_phase: dict[str, list[str]] = {}
        for interaction in open_interactions:
            if interaction.required_by_step_id:
                phase = program_plan.phase_for_step(interaction.required_by_step_id)
                if phase is not None:
                    interactions_by_phase.setdefault(phase.id, []).append(interaction.id)

        phase_runs: dict[str, PhaseRun] = {}
        for phase in program_plan.phases:
            runs = [step_runs[step_id] for step_id in phase.step_ids]
            completed = tuple(run.id for run in runs if run.status is StepStatus.PASSED)
            failed = tuple(run.id for run in runs if run.status is StepStatus.FAILED)
            blocked = tuple(
                run.id
                for run in runs
                if self._has_intervention_block(run)
            )
            running = tuple(run.id for run in runs if run.status is StepStatus.RUNNING)
            status = self._phase_status(runs)
            phase_runs[phase.id] = PhaseRun(
                id=phase.id,
                title=phase.title,
                status=status,
                step_ids=phase.step_ids,
                completed_step_ids=completed,
                blocked_step_ids=blocked,
                failed_step_ids=failed,
                running_step_ids=running,
                pending_interaction_ids=tuple(interactions_by_phase.get(phase.id, ())),
                checkpoint_id=latest_checkpoint.id if latest_checkpoint else None,
                metadata={"acceptance_refs": list(phase.acceptance_refs)},
            )
        return phase_runs

    def _phase_status(self, runs: list[StepRun]) -> PhaseStatus:
        if any(run.status is StepStatus.FAILED for run in runs):
            return PhaseStatus.FAILED
        if any(self._has_intervention_block(run) for run in runs):
            return PhaseStatus.BLOCKED
        if runs and all(run.status is StepStatus.PASSED for run in runs):
            return PhaseStatus.PASSED
        if any(run.status in {StepStatus.RUNNING, StepStatus.PASSED} for run in runs):
            return PhaseStatus.RUNNING
        return PhaseStatus.PENDING

    def _has_intervention_block(self, run: StepRun) -> bool:
        return (
            run.status is StepStatus.BLOCKED
            or any(
                not reason.startswith("dependency:")
                for reason in run.blocked_by
            )
        )
