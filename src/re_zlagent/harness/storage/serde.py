"""Serialization helpers for task storage adapters."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from re_zlagent.harness.tasking import (
    AcceptanceCriterion,
    ArtifactKind,
    ArtifactRecord,
    Checkpoint,
    CheckpointStatus,
    CriterionType,
    FailureDuration,
    FailureEnvelope,
    FailureType,
    FailureVisibility,
    InteractionKind,
    InteractionStatus,
    PendingInteraction,
    PlanDAG,
    PlanStep,
    ProgramPhase,
    ProgramPlan,
    RecoveryAction,
    RunLease,
    RunLeaseState,
    SideEffectRecord,
    SideEffectStatus,
    TaskContract,
    TaskEvent,
    TaskEventType,
    TaskRun,
    TaskRunStatus,
)
from re_zlagent.harness.tools import Evidence, SideEffect


def datetime_to_str(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def datetime_from_str(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def evidence_to_dict(item: Evidence) -> dict[str, Any]:
    return item.to_dict()


def evidence_from_dict(data: dict[str, Any]) -> Evidence:
    return Evidence(
        type=str(data["type"]),
        ref=str(data["ref"]),
        summary=str(data.get("summary") or ""),
        metadata=dict(data.get("metadata") or {}),
    )


def side_effect_to_dict(item: SideEffect) -> dict[str, Any]:
    return item.to_dict()


def side_effect_from_dict(data: dict[str, Any]) -> SideEffect:
    return SideEffect(
        type=str(data["type"]),
        target=str(data["target"]),
        risk=str(data.get("risk") or "low"),
        metadata=dict(data.get("metadata") or {}),
    )


def criterion_to_dict(item: AcceptanceCriterion) -> dict[str, Any]:
    return {
        "id": item.id,
        "description": item.description,
        "type": item.type.value,
        "required": item.required,
        "evidence_refs": list(item.evidence_refs),
        "freshness_window_seconds": item.freshness_window_seconds,
        "metadata": dict(item.metadata),
    }


def criterion_from_dict(data: dict[str, Any]) -> AcceptanceCriterion:
    return AcceptanceCriterion(
        id=str(data["id"]),
        description=str(data["description"]),
        type=CriterionType(str(data["type"])),
        required=bool(data.get("required", True)),
        evidence_refs=tuple(str(item) for item in data.get("evidence_refs") or ()),
        freshness_window_seconds=data.get("freshness_window_seconds"),
        metadata=dict(data.get("metadata") or {}),
    )


def contract_to_dict(item: TaskContract) -> dict[str, Any]:
    return {
        "id": item.id,
        "user_goal": item.user_goal,
        "stakeholders": list(item.stakeholders),
        "mvp_scope": list(item.mvp_scope),
        "out_of_scope": list(item.out_of_scope),
        "acceptance_criteria": [
            criterion_to_dict(criterion)
            for criterion in item.acceptance_criteria
        ],
        "capability_boundaries": dict(item.capability_boundaries),
        "freshness_policy": dict(item.freshness_policy),
        "created_at": datetime_to_str(item.created_at),
        "version": item.version,
    }


def contract_from_dict(data: dict[str, Any]) -> TaskContract:
    return TaskContract(
        id=str(data["id"]),
        user_goal=str(data["user_goal"]),
        stakeholders=tuple(str(item) for item in data.get("stakeholders") or ()),
        mvp_scope=tuple(str(item) for item in data.get("mvp_scope") or ()),
        out_of_scope=tuple(str(item) for item in data.get("out_of_scope") or ()),
        acceptance_criteria=tuple(
            criterion_from_dict(item)
            for item in data.get("acceptance_criteria") or ()
        ),
        capability_boundaries=dict(data.get("capability_boundaries") or {}),
        freshness_policy=dict(data.get("freshness_policy") or {}),
        created_at=(
            datetime_from_str(data.get("created_at"))
            or datetime.now(timezone.utc)
        ),
        version=str(data.get("version") or "1"),
    )


def plan_step_to_dict(item: PlanStep) -> dict[str, Any]:
    return {
        "id": item.id,
        "title": item.title,
        "expected_output": item.expected_output,
        "verification": item.verification,
        "depends_on": list(item.depends_on),
        "required_evidence_refs": list(item.required_evidence_refs),
        "metadata": dict(item.metadata),
    }


def plan_step_from_dict(data: dict[str, Any]) -> PlanStep:
    return PlanStep(
        id=str(data["id"]),
        title=str(data["title"]),
        expected_output=str(data.get("expected_output") or ""),
        verification=str(data.get("verification") or ""),
        depends_on=tuple(str(item) for item in data.get("depends_on") or ()),
        required_evidence_refs=tuple(
            str(item) for item in data.get("required_evidence_refs") or ()
        ),
        metadata=dict(data.get("metadata") or {}),
    )


def program_phase_to_dict(item: ProgramPhase) -> dict[str, Any]:
    return item.to_dict()


def program_phase_from_dict(data: dict[str, Any]) -> ProgramPhase:
    return ProgramPhase(
        id=str(data["id"]),
        title=str(data["title"]),
        step_ids=tuple(str(item) for item in data.get("step_ids") or ()),
        acceptance_refs=tuple(
            str(item) for item in data.get("acceptance_refs") or ()
        ),
        metadata=dict(data.get("metadata") or {}),
    )


def program_plan_to_dict(item: ProgramPlan) -> dict[str, Any]:
    return {
        "id": item.id,
        "contract_id": item.contract_id,
        "steps": [plan_step_to_dict(step) for step in item.dag.steps],
        "phases": [program_phase_to_dict(phase) for phase in item.phases],
        "metadata": dict(item.metadata),
    }


def program_plan_from_dict(data: dict[str, Any]) -> ProgramPlan:
    return ProgramPlan(
        id=str(data["id"]),
        contract_id=str(data["contract_id"]),
        dag=PlanDAG(
            tuple(plan_step_from_dict(item) for item in data.get("steps") or ())
        ),
        phases=tuple(
            program_phase_from_dict(item) for item in data.get("phases") or ()
        ),
        metadata=dict(data.get("metadata") or {}),
    )


def run_to_dict(item: TaskRun) -> dict[str, Any]:
    return {
        "id": item.id,
        "contract_id": item.contract_id,
        "plan_id": item.plan_id,
        "status": item.status.value,
        "current_checkpoint_id": item.current_checkpoint_id,
        "event_seq": item.event_seq,
        "model_name": item.model_name,
        "prompt_version": item.prompt_version,
        "metadata": dict(item.metadata),
    }


def run_from_dict(data: dict[str, Any]) -> TaskRun:
    return TaskRun(
        id=str(data["id"]),
        contract_id=str(data["contract_id"]),
        plan_id=data.get("plan_id"),
        status=TaskRunStatus(str(data.get("status") or TaskRunStatus.CREATED.value)),
        current_checkpoint_id=data.get("current_checkpoint_id"),
        event_seq=int(data.get("event_seq") or 0),
        model_name=data.get("model_name"),
        prompt_version=data.get("prompt_version"),
        metadata=dict(data.get("metadata") or {}),
    )


def event_to_dict(item: TaskEvent) -> dict[str, Any]:
    return item.to_dict()


def event_from_dict(data: dict[str, Any]) -> TaskEvent:
    return TaskEvent(
        id=str(data["id"]),
        run_id=str(data["run_id"]),
        seq=int(data["seq"]),
        type=TaskEventType(str(data["type"])),
        payload=dict(data.get("payload") or {}),
        evidence=tuple(
            evidence_from_dict(item)
            for item in data.get("evidence") or ()
        ),
        side_effects=tuple(
            side_effect_from_dict(item)
            for item in data.get("side_effects") or ()
        ),
        created_at=datetime_from_str(data.get("created_at")) or datetime.fromisoformat("1970-01-01T00:00:00+00:00"),
        idempotency_key=data.get("idempotency_key"),
    )


def failure_to_dict(item: FailureEnvelope) -> dict[str, Any]:
    return item.to_dict()


def failure_from_dict(data: dict[str, Any]) -> FailureEnvelope:
    return FailureEnvelope(
        status=TaskRunStatus(str(data["status"])),
        failed_step=str(data["failed_step"]),
        failure_type=FailureType(str(data["failure_type"])),
        root_cause=str(data["root_cause"]),
        recoverable=bool(data["recoverable"]),
        recommended_action=RecoveryAction(str(data["recommended_action"])),
        checkpoint_id=data.get("checkpoint_id"),
        evidence=tuple(
            evidence_from_dict(item)
            for item in data.get("evidence") or ()
        ),
        side_effects=tuple(
            side_effect_from_dict(item)
            for item in data.get("side_effects") or ()
        ),
        metadata=dict(data.get("metadata") or {}),
        visibility=FailureVisibility(str(data.get("visibility") or FailureVisibility.EXPLICIT.value)),
        duration=FailureDuration(str(data.get("duration") or FailureDuration.UNKNOWN.value)),
    )


def checkpoint_to_dict(item: Checkpoint) -> dict[str, Any]:
    return item.to_dict()


def checkpoint_from_dict(data: dict[str, Any]) -> Checkpoint:
    failure_data = data.get("failure")
    return Checkpoint(
        id=str(data["id"]),
        run_id=str(data["run_id"]),
        seq=int(data["seq"]),
        status=CheckpointStatus(str(data["status"])),
        state=dict(data.get("state") or {}),
        resume_from_event_id=data.get("resume_from_event_id"),
        failure=failure_from_dict(failure_data) if failure_data else None,
        created_at=datetime_from_str(data.get("created_at")) or datetime.fromisoformat("1970-01-01T00:00:00+00:00"),
    )


def pending_interaction_to_dict(item: PendingInteraction) -> dict[str, Any]:
    return item.to_dict()


def pending_interaction_from_dict(data: dict[str, Any]) -> PendingInteraction:
    return PendingInteraction(
        id=str(data["id"]),
        run_id=str(data["run_id"]),
        checkpoint_id=str(data["checkpoint_id"]),
        question=str(data["question"]),
        required_by_step_id=data.get("required_by_step_id"),
        kind=InteractionKind(str(data.get("kind") or InteractionKind.USER_INPUT.value)),
        status=InteractionStatus(str(data.get("status") or InteractionStatus.OPEN.value)),
        resume_token=str(data["resume_token"]),
        answer=data.get("answer"),
        created_at=datetime_from_str(data.get("created_at")) or datetime.fromisoformat("1970-01-01T00:00:00+00:00"),
        expires_at=datetime_from_str(data.get("expires_at")),
        resolved_at=datetime_from_str(data.get("resolved_at")),
        metadata=dict(data.get("metadata") or {}),
    )


def artifact_to_dict(item: ArtifactRecord) -> dict[str, Any]:
    return item.to_dict()


def artifact_from_dict(data: dict[str, Any]) -> ArtifactRecord:
    return ArtifactRecord(
        id=str(data["id"]),
        run_id=str(data["run_id"]),
        ref=str(data["ref"]),
        kind=ArtifactKind(str(data["kind"])),
        producer_step_id=data.get("producer_step_id"),
        evidence_refs=tuple(str(item) for item in data.get("evidence_refs") or ()),
        created_at=datetime_from_str(data.get("created_at")) or datetime.fromisoformat("1970-01-01T00:00:00+00:00"),
        metadata=dict(data.get("metadata") or {}),
    )


def side_effect_record_to_dict(item: SideEffectRecord) -> dict[str, Any]:
    return item.to_dict()


def side_effect_record_from_dict(data: dict[str, Any]) -> SideEffectRecord:
    return SideEffectRecord(
        id=str(data["id"]),
        run_id=str(data["run_id"]),
        idempotency_key=str(data["idempotency_key"]),
        type=str(data["type"]),
        target=str(data["target"]),
        status=SideEffectStatus(str(data.get("status") or SideEffectStatus.PLANNED.value)),
        intent_fingerprint=str(data.get("intent_fingerprint") or ""),
        producer_step_id=data.get("producer_step_id"),
        attempt_count=int(data.get("attempt_count") or 0),
        created_at=datetime_from_str(data.get("created_at")) or datetime.fromisoformat("1970-01-01T00:00:00+00:00"),
        updated_at=(
            datetime_from_str(data.get("updated_at"))
            or datetime_from_str(data.get("created_at"))
            or datetime.fromisoformat("1970-01-01T00:00:00+00:00")
        ),
        metadata=dict(data.get("metadata") or {}),
    )


def run_lease_to_dict(item: RunLease) -> dict[str, Any]:
    return item.to_dict()


def run_lease_from_dict(data: dict[str, Any]) -> RunLease:
    return RunLease(
        run_id=str(data["run_id"]),
        state=RunLeaseState(
            str(data.get("state") or RunLeaseState.AVAILABLE.value)
        ),
        owner_id=data.get("owner_id"),
        lease_token=data.get("lease_token"),
        acquired_at=datetime_from_str(data.get("acquired_at")),
        heartbeat_at=datetime_from_str(data.get("heartbeat_at")),
        expires_at=datetime_from_str(data.get("expires_at")),
        attempt_count=int(data.get("attempt_count") or 0),
        retry_budget=int(data.get("retry_budget") or 3),
        next_attempt_at=datetime_from_str(data.get("next_attempt_at")),
        last_error=data.get("last_error"),
        release_reason=data.get("release_reason"),
        version=int(data.get("version") or 0),
        metadata=dict(data.get("metadata") or {}),
    )
