"""Context packs for resuming long-running tasks."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from re_zlagent.harness.context import (
    ContextInput,
    ContextManifest,
    ContextManifestBuilder,
    ContextTrust,
)
from re_zlagent.harness.storage import LongTaskStore, TaskStore
from re_zlagent.harness.tasking import (
    AcceptanceCriterion,
    ArtifactRecord,
    DagExecutionPolicy,
    LongTaskProjector,
    PendingInteraction,
    ProgramPlan,
    TaskContract,
    TaskEvent,
    TaskEventType,
)


@dataclass(frozen=True, slots=True)
class AcceptanceContext:
    """Acceptance state included in a resume context pack."""

    accepted: bool | None = None
    passed_criteria: tuple[str, ...] = field(default_factory=tuple)
    failed_criteria: tuple[str, ...] = field(default_factory=tuple)
    blocked_criteria: tuple[str, ...] = field(default_factory=tuple)
    pending_criteria: tuple[str, ...] = field(default_factory=tuple)
    reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "passed_criteria", tuple(self.passed_criteria))
        object.__setattr__(self, "failed_criteria", tuple(self.failed_criteria))
        object.__setattr__(self, "blocked_criteria", tuple(self.blocked_criteria))
        object.__setattr__(self, "pending_criteria", tuple(self.pending_criteria))

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "passed_criteria": list(self.passed_criteria),
            "failed_criteria": list(self.failed_criteria),
            "blocked_criteria": list(self.blocked_criteria),
            "pending_criteria": list(self.pending_criteria),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ContextPack:
    """Compact, structured state used to resume after context loss."""

    run_id: str
    contract_id: str
    program_plan_id: str
    event_seq: int
    checkpoint_id: str | None
    checkpoint_status: str | None
    current_phase_ids: tuple[str, ...] = field(default_factory=tuple)
    frontier_step_ids: tuple[str, ...] = field(default_factory=tuple)
    completed_step_ids: tuple[str, ...] = field(default_factory=tuple)
    failed_step_ids: tuple[str, ...] = field(default_factory=tuple)
    blocked_step_ids: tuple[str, ...] = field(default_factory=tuple)
    pending_interactions: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    artifact_records: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    evidence_refs: tuple[str, ...] = field(default_factory=tuple)
    acceptance: AcceptanceContext = field(default_factory=AcceptanceContext)
    recent_events: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    manifest: ContextManifest | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.run_id.strip():
            raise ValueError("context_pack.run_id must be non-empty")
        if not self.contract_id.strip():
            raise ValueError("context_pack.contract_id must be non-empty")
        if not self.program_plan_id.strip():
            raise ValueError("context_pack.program_plan_id must be non-empty")
        if self.event_seq < 0:
            raise ValueError("context_pack.event_seq must be >= 0")
        object.__setattr__(self, "current_phase_ids", tuple(self.current_phase_ids))
        object.__setattr__(self, "frontier_step_ids", tuple(self.frontier_step_ids))
        object.__setattr__(self, "completed_step_ids", tuple(self.completed_step_ids))
        object.__setattr__(self, "failed_step_ids", tuple(self.failed_step_ids))
        object.__setattr__(self, "blocked_step_ids", tuple(self.blocked_step_ids))
        object.__setattr__(self, "pending_interactions", tuple(self.pending_interactions))
        object.__setattr__(self, "artifact_records", tuple(self.artifact_records))
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        object.__setattr__(self, "recent_events", tuple(self.recent_events))
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "contract_id": self.contract_id,
            "program_plan_id": self.program_plan_id,
            "event_seq": self.event_seq,
            "checkpoint_id": self.checkpoint_id,
            "checkpoint_status": self.checkpoint_status,
            "current_phase_ids": list(self.current_phase_ids),
            "frontier_step_ids": list(self.frontier_step_ids),
            "completed_step_ids": list(self.completed_step_ids),
            "failed_step_ids": list(self.failed_step_ids),
            "blocked_step_ids": list(self.blocked_step_ids),
            "pending_interactions": [dict(item) for item in self.pending_interactions],
            "artifact_records": [dict(item) for item in self.artifact_records],
            "evidence_refs": list(self.evidence_refs),
            "acceptance": self.acceptance.to_dict(),
            "recent_events": [dict(item) for item in self.recent_events],
            "context_manifest": (
                self.manifest.to_dict() if self.manifest is not None else None
            ),
            "metadata": dict(self.metadata),
        }


class ContextPackBuilder:
    """Build a compact resume context from durable runtime state."""

    def __init__(
        self,
        store: TaskStore,
        *,
        dag_execution_policy: DagExecutionPolicy | None = None,
        long_task_store: LongTaskStore | None = None,
        projector: LongTaskProjector | None = None,
        max_context_chars: int = 8_000,
    ) -> None:
        self._store = store
        self._long_task_store = long_task_store
        self._dag_execution_policy = dag_execution_policy
        self._projector = projector or LongTaskProjector()
        self._context_builder = ContextManifestBuilder(
            max_chars=max_context_chars
        )

    def build(
        self,
        *,
        run_id: str,
        program_plan: ProgramPlan,
        pending_interactions: tuple[PendingInteraction, ...] | list[PendingInteraction] | None = None,
        artifact_records: tuple[ArtifactRecord, ...] | list[ArtifactRecord] | None = None,
        max_recent_events: int = 12,
    ) -> ContextPack:
        if max_recent_events <= 0:
            raise ValueError("max_recent_events must be > 0")
        run = self._store.get_run(run_id)
        if run is None:
            raise ValueError(f"unknown run id: {run_id}")
        contract = self._store.get_contract(run.contract_id)
        if contract is None:
            raise ValueError(f"unknown contract id: {run.contract_id}")
        if contract.id != program_plan.contract_id:
            raise ValueError("program plan does not match run contract")
        if run.plan_id is not None and run.plan_id != program_plan.id:
            raise ValueError("program plan does not match run plan revision")

        events = self._store.list_events(run_id)
        checkpoints = self._store.list_checkpoints(run_id)
        interaction_records = self._pending_interactions(
            run_id,
            pending_interactions,
        )
        artifacts = self._artifact_records(run_id, artifact_records)
        projection = self._projector.project(
            run_id=run_id,
            program_plan=program_plan,
            events=events,
            checkpoints=checkpoints,
            pending_interactions=interaction_records,
        )
        dag_execution = (
            self._dag_execution_policy.assess(
                program_plan=program_plan,
                projection=projection,
            ).to_dict()
            if self._dag_execution_policy is not None
            else None
        )
        current_phase_ids = tuple(
            phase_id
            for phase_id, phase in projection.phase_runs.items()
            if phase.status.value in {"pending", "running", "blocked", "failed"}
        )
        pending_payload = tuple(
            item.to_dict()
            for item in interaction_records
            if item.run_id == run_id and item.open
        )
        artifact_payload = tuple(
            item.to_dict()
            for item in artifacts
            if item.run_id == run_id
        )
        evidence_refs = self._evidence_refs(events)
        acceptance = self._acceptance_context(contract, events)
        recent_events = tuple(
            self._compact_event(event)
            for event in events[-max_recent_events:]
        )
        state_payload = {
            "current_phase_ids": current_phase_ids,
            "frontier_step_ids": projection.frontier_step_ids,
            "completed_step_ids": projection.completed_step_ids,
            "failed_step_ids": projection.failed_step_ids,
            "blocked_step_ids": projection.blocked_step_ids,
        }
        manifest = self._context_builder.build(
            (
                self._context_input(
                    id="task_identity",
                    source="task_store",
                    content={
                        "run_id": run_id,
                        "contract_id": contract.id,
                        "program_plan_id": program_plan.id,
                        "user_goal": contract.user_goal,
                    },
                    max_chars=1_500,
                ),
                self._context_input(
                    id="task_state",
                    source="runtime_projection",
                    content=state_payload,
                    max_chars=1_500,
                ),
                self._context_input(
                    id="pending_interactions",
                    source="long_task_store",
                    content=pending_payload,
                    max_chars=1_500,
                ),
                self._context_input(
                    id="artifacts_and_evidence",
                    source="runtime_evidence",
                    content={
                        "artifacts": artifact_payload,
                        "evidence_refs": evidence_refs,
                    },
                    max_chars=1_500,
                ),
                self._context_input(
                    id="acceptance",
                    source="acceptance_gate",
                    content=acceptance.to_dict(),
                    max_chars=1_000,
                ),
                self._context_input(
                    id="recent_events",
                    source="task_event_log",
                    content=recent_events,
                    max_chars=2_000,
                ),
            )
        )
        return ContextPack(
            run_id=run_id,
            contract_id=contract.id,
            program_plan_id=program_plan.id,
            event_seq=run.event_seq,
            checkpoint_id=projection.latest_checkpoint_id,
            checkpoint_status=projection.latest_checkpoint_status,
            current_phase_ids=current_phase_ids,
            frontier_step_ids=projection.frontier_step_ids,
            completed_step_ids=projection.completed_step_ids,
            failed_step_ids=projection.failed_step_ids,
            blocked_step_ids=projection.blocked_step_ids,
            pending_interactions=pending_payload,
            artifact_records=artifact_payload,
            evidence_refs=evidence_refs,
            acceptance=acceptance,
            recent_events=recent_events,
            manifest=manifest,
            metadata={
                "run_status": run.status.value,
                "model_name": run.model_name,
                "prompt_version": run.prompt_version,
                "step_statuses": {
                    step_id: step.status.value
                    for step_id, step in projection.step_runs.items()
                },
                "dag_execution": dag_execution,
            },
        )

    @staticmethod
    def _context_input(
        *,
        id: str,
        source: str,
        content: Any,
        max_chars: int,
    ) -> ContextInput:
        return ContextInput(
            id=id,
            source=source,
            trust=ContextTrust.RUNTIME_STATE,
            content=json.dumps(
                content,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ),
            max_chars=max_chars,
        )

    def _pending_interactions(
        self,
        run_id: str,
        supplied: tuple[PendingInteraction, ...] | list[PendingInteraction] | None,
    ) -> tuple[PendingInteraction, ...]:
        if supplied is not None:
            return tuple(supplied)
        if self._long_task_store is None:
            return ()
        return self._long_task_store.list_pending_interactions(run_id)

    def _artifact_records(
        self,
        run_id: str,
        supplied: tuple[ArtifactRecord, ...] | list[ArtifactRecord] | None,
    ) -> tuple[ArtifactRecord, ...]:
        if supplied is not None:
            return tuple(supplied)
        if self._long_task_store is None:
            return ()
        return self._long_task_store.list_artifacts(run_id)

    def _acceptance_context(
        self,
        contract: TaskContract,
        events: tuple[TaskEvent, ...],
    ) -> AcceptanceContext:
        for event in reversed(events):
            if event.type is not TaskEventType.ACCEPTANCE_EVALUATED:
                continue
            passed = tuple(str(item) for item in event.payload.get("passed_criteria") or ())
            failed = tuple(str(item) for item in event.payload.get("failed_criteria") or ())
            blocked = tuple(str(item) for item in event.payload.get("blocked_criteria") or ())
            known = set(passed).union(failed).union(blocked)
            pending = tuple(
                criterion.id
                for criterion in contract.acceptance_criteria
                if criterion.id not in known
            )
            accepted = event.payload.get("accepted")
            return AcceptanceContext(
                accepted=accepted if isinstance(accepted, bool) else None,
                passed_criteria=passed,
                failed_criteria=failed,
                blocked_criteria=blocked,
                pending_criteria=pending,
                reason=str(event.payload.get("reason") or ""),
            )

        return AcceptanceContext(
            pending_criteria=tuple(
                criterion.id
                for criterion in contract.acceptance_criteria
                if self._required(criterion)
            ),
            reason="acceptance has not been evaluated",
        )

    def _evidence_refs(self, events: tuple[TaskEvent, ...]) -> tuple[str, ...]:
        refs: set[str] = set()
        for event in events:
            refs.update(item.ref for item in event.evidence)
            payload_refs = event.payload.get("evidence_refs") or ()
            refs.update(str(item) for item in payload_refs)
        return tuple(sorted(refs))

    def _compact_event(self, event: TaskEvent) -> dict[str, Any]:
        return {
            "id": event.id,
            "seq": event.seq,
            "type": event.type.value,
            "payload": self._compact_payload(event.payload),
            "evidence_refs": [item.ref for item in event.evidence],
            "side_effect_count": len(event.side_effects),
            "created_at": event.created_at.isoformat(),
        }

    def _compact_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        compact = dict(payload)
        content = compact.get("content")
        if isinstance(content, str) and len(content) > 500:
            compact["content"] = f"{content[:500]}...[truncated]"
        return compact

    @staticmethod
    def _required(criterion: AcceptanceCriterion) -> bool:
        return criterion.required
