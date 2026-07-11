"""Crash-safe side-effect outbox coordination."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from re_zlagent.harness.storage import LongTaskStore
from re_zlagent.harness.tasking import SideEffectRecord, SideEffectStatus
from re_zlagent.harness.tools import (
    Evidence,
    PreparedToolCall,
    RecommendedNextAction,
    SideEffect,
    ToolErrorType,
    ToolResult,
    ToolResultStatus,
)


class OutboxFaultPoint(str, Enum):
    """Deterministic crash-injection points around external dispatch."""

    AFTER_INTENT_PERSISTED = "after_intent_persisted"
    AFTER_DISPATCH = "after_dispatch"
    BEFORE_RESULT_COMMIT = "before_result_commit"


class OutboxFaultInjector(Protocol):
    """Fault-injection boundary used by reliability tests."""

    def hit(
        self,
        point: OutboxFaultPoint,
        records: tuple[SideEffectRecord, ...],
    ) -> None:
        """Raise at configured points or return without mutation."""


class NoopOutboxFaultInjector:
    """Production default that never injects a crash."""

    def hit(
        self,
        point: OutboxFaultPoint,
        records: tuple[SideEffectRecord, ...],
    ) -> None:
        return None


class InjectedOutboxCrash(RuntimeError):
    """Process-crash surrogate raised only by explicit test injectors."""


class OutboxAction(str, Enum):
    """Decision after loading or creating durable intents."""

    BYPASS = "bypass"
    DISPATCH = "dispatch"
    REPLAY_RESULT = "replay_result"
    BLOCK = "block"


@dataclass(frozen=True, slots=True)
class OutboxPreparation:
    """Prepared outbox state for one logical tool invocation."""

    action: OutboxAction
    records: tuple[SideEffectRecord, ...] = field(default_factory=tuple)
    result: ToolResult | None = None
    retry_safe: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "records", tuple(self.records))


@dataclass(frozen=True, slots=True)
class OutboxReconciliationReport:
    """Read model for operator-side outbox reconciliation."""

    run_id: str
    retryable_ids: tuple[str, ...] = field(default_factory=tuple)
    pending_result_commit_ids: tuple[str, ...] = field(default_factory=tuple)
    manual_review_ids: tuple[str, ...] = field(default_factory=tuple)
    confirmed_ids: tuple[str, ...] = field(default_factory=tuple)
    reverted_ids: tuple[str, ...] = field(default_factory=tuple)

    @property
    def requires_manual_review(self) -> bool:
        return bool(self.manual_review_ids)


class SideEffectOutbox:
    """Coordinate durable intent, dispatch, result commit, and replay."""

    def __init__(
        self,
        store: LongTaskStore | None,
        *,
        fault_injector: OutboxFaultInjector | None = None,
    ) -> None:
        self._store = store
        self._fault_injector = fault_injector or NoopOutboxFaultInjector()

    def prepare(
        self,
        *,
        run_id: str,
        plan_id: str,
        step_id: str,
        call: PreparedToolCall,
    ) -> OutboxPreparation:
        if not call.outbox_required:
            return OutboxPreparation(action=OutboxAction.BYPASS)
        if self._store is None:
            return OutboxPreparation(
                action=OutboxAction.BLOCK,
                result=self._manual_review_result(
                    "outbox-required tool cannot run without durable LongTaskStore"
                ),
            )

        records = tuple(
            self._store.save_side_effect(
                self._new_record(
                    run_id=run_id,
                    plan_id=plan_id,
                    step_id=step_id,
                    call=call,
                    index=index,
                    intent=intent,
                )
            )
            for index, intent in enumerate(call.side_effect_intents)
        )
        statuses = {record.status for record in records}

        if statuses.issubset(
            {SideEffectStatus.APPLIED, SideEffectStatus.CONFIRMED}
        ):
            replay = self._replay_result(records)
            if replay is not None:
                return OutboxPreparation(
                    action=OutboxAction.REPLAY_RESULT,
                    records=records,
                    result=replay,
                    retry_safe=call.side_effect_retry_safe,
                )
            records = self._mark_uncertain(
                records,
                reason="applied outbox record has no replayable tool result",
            )
            return OutboxPreparation(
                action=OutboxAction.BLOCK,
                records=records,
                result=self._manual_review_result(
                    "side-effect result cannot be reconstructed safely"
                ),
            )

        if SideEffectStatus.UNCERTAIN in statuses:
            return OutboxPreparation(
                action=OutboxAction.BLOCK,
                records=records,
                result=self._manual_review_result(
                    "side-effect outcome is uncertain and requires reconciliation"
                ),
            )

        if SideEffectStatus.DISPATCHING in statuses:
            if not call.side_effect_retry_safe:
                records = self._mark_uncertain(
                    records,
                    reason="process stopped while a non-idempotent action was dispatching",
                )
                return OutboxPreparation(
                    action=OutboxAction.BLOCK,
                    records=records,
                    result=self._manual_review_result(
                        "non-idempotent side effect may already have been applied"
                    ),
                )
            records = self._reset_for_idempotent_retry(records)

        if SideEffectStatus.FAILED in {record.status for record in records}:
            manually_cleared = all(
                bool(record.metadata.get("reconciled_manually"))
                for record in records
                if record.status is SideEffectStatus.FAILED
            )
            if not call.side_effect_retry_safe and not manually_cleared:
                return OutboxPreparation(
                    action=OutboxAction.BLOCK,
                    records=records,
                    result=self._manual_review_result(
                        "failed side effect is not safe to retry automatically"
                    ),
                )
            records = self._reset_failed(records)

        if all(record.status is SideEffectStatus.PLANNED for record in records):
            return OutboxPreparation(
                action=OutboxAction.DISPATCH,
                records=records,
                retry_safe=call.side_effect_retry_safe,
            )

        records = self._mark_uncertain(
            records,
            reason="outbox records have inconsistent states",
        )
        return OutboxPreparation(
            action=OutboxAction.BLOCK,
            records=records,
            result=self._manual_review_result(
                "outbox records have inconsistent states"
            ),
        )

    def after_intent_persisted(self, preparation: OutboxPreparation) -> None:
        if preparation.action is OutboxAction.DISPATCH:
            self._fault_injector.hit(
                OutboxFaultPoint.AFTER_INTENT_PERSISTED,
                preparation.records,
            )

    def begin_dispatch(
        self,
        preparation: OutboxPreparation,
    ) -> OutboxPreparation:
        if preparation.action is not OutboxAction.DISPATCH:
            return preparation
        records = tuple(
            self._transition(
                record,
                expected=SideEffectStatus.PLANNED,
                status=SideEffectStatus.DISPATCHING,
            )
            for record in preparation.records
        )
        return OutboxPreparation(
            action=preparation.action,
            records=records,
            retry_safe=preparation.retry_safe,
        )

    def after_dispatch(self, preparation: OutboxPreparation) -> None:
        if preparation.action is OutboxAction.DISPATCH:
            self._fault_injector.hit(
                OutboxFaultPoint.AFTER_DISPATCH,
                preparation.records,
            )

    def record_result(
        self,
        preparation: OutboxPreparation,
        result: ToolResult,
        *,
        expected_intents: tuple[SideEffect, ...],
    ) -> tuple[OutboxPreparation, ToolResult]:
        if preparation.action is not OutboxAction.DISPATCH:
            return preparation, result

        if result.ok and not self._side_effects_match(
            result.side_effects,
            expected_intents,
        ):
            records = self._mark_uncertain(
                preparation.records,
                reason="tool result side effects differ from durable intents",
            )
            blocked = self._manual_review_result(
                "tool violated its declared side-effect intent",
                evidence=result.evidence,
                side_effects=result.side_effects,
            )
            return (
                OutboxPreparation(
                    action=OutboxAction.BLOCK,
                    records=records,
                    result=blocked,
                ),
                blocked,
            )

        snapshot = self._result_to_dict(result)
        if result.ok:
            status = SideEffectStatus.APPLIED
        elif preparation.retry_safe and not result.side_effects:
            status = SideEffectStatus.FAILED
        else:
            status = SideEffectStatus.UNCERTAIN

        records = tuple(
            self._transition(
                record,
                expected=SideEffectStatus.DISPATCHING,
                status=status,
                metadata={
                    "tool_result": snapshot,
                    "dispatch_error": result.error,
                },
            )
            for record in preparation.records
        )
        if status is SideEffectStatus.UNCERTAIN:
            result = self._manual_review_result(
                "side-effect dispatch returned an uncertain outcome",
                evidence=result.evidence,
                side_effects=result.side_effects,
            )
        return (
            OutboxPreparation(
                action=(
                    preparation.action
                    if status is not SideEffectStatus.UNCERTAIN
                    else OutboxAction.BLOCK
                ),
                records=records,
                result=result,
                retry_safe=preparation.retry_safe,
            ),
            result,
        )

    def before_result_commit(self, preparation: OutboxPreparation) -> None:
        if preparation.records:
            self._fault_injector.hit(
                OutboxFaultPoint.BEFORE_RESULT_COMMIT,
                preparation.records,
            )

    def confirm_result(
        self,
        preparation: OutboxPreparation,
        *,
        result_event_id: str,
    ) -> OutboxPreparation:
        records: list[SideEffectRecord] = []
        for record in preparation.records:
            if record.status is SideEffectStatus.APPLIED:
                record = self._transition(
                    record,
                    expected=SideEffectStatus.APPLIED,
                    status=SideEffectStatus.CONFIRMED,
                    metadata={"result_event_id": result_event_id},
                )
            records.append(record)
        return OutboxPreparation(
            action=preparation.action,
            records=tuple(records),
            result=preparation.result,
            retry_safe=preparation.retry_safe,
        )

    def record_unplanned_result(
        self,
        *,
        run_id: str,
        plan_id: str,
        step_id: str,
        call: PreparedToolCall,
        result: ToolResult,
    ) -> tuple[OutboxPreparation, ToolResult]:
        """Quarantine side effects emitted without a prepared durable intent."""

        if not result.side_effects:
            return OutboxPreparation(action=OutboxAction.BYPASS), result
        if self._store is None:
            return (
                OutboxPreparation(
                    action=OutboxAction.BLOCK,
                    result=self._manual_review_result(
                        "tool emitted an unplanned side effect without durable storage"
                    ),
                ),
                self._manual_review_result(
                    "tool emitted an unplanned side effect without durable storage",
                    evidence=result.evidence,
                    side_effects=result.side_effects,
                ),
            )

        records: list[SideEffectRecord] = []
        for index, side_effect in enumerate(result.side_effects):
            key = f"{call.context.idempotency_key}:unplanned:{index}"
            canonical = json.dumps(
                {
                    "tool_name": call.tool_name,
                    "arguments": call.arguments,
                    "index": index,
                    "observed": side_effect.to_dict(),
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                default=str,
            ).encode("utf-8")
            record_hash = hashlib.sha256(
                f"{run_id}:{key}".encode("utf-8")
            ).hexdigest()
            records.append(
                self._store.save_side_effect(
                    SideEffectRecord(
                        id=f"side_{record_hash[:24]}",
                        run_id=run_id,
                        idempotency_key=key,
                        type=side_effect.type,
                        target=side_effect.target,
                        status=SideEffectStatus.UNCERTAIN,
                        intent_fingerprint=(
                            f"sha256:{hashlib.sha256(canonical).hexdigest()}"
                        ),
                        producer_step_id=step_id,
                        metadata={
                            "program_plan_id": plan_id,
                            "tool_name": call.tool_name,
                            "uncertain_reason": "side effect was not declared before dispatch",
                            "tool_result": self._result_to_dict(result),
                        },
                    )
                )
            )
        blocked = self._manual_review_result(
            "tool emitted side effects outside the outbox contract",
            evidence=result.evidence,
            side_effects=result.side_effects,
        )
        return (
            OutboxPreparation(
                action=OutboxAction.BLOCK,
                records=tuple(records),
                result=blocked,
            ),
            blocked,
        )

    def _new_record(
        self,
        *,
        run_id: str,
        plan_id: str,
        step_id: str,
        call: PreparedToolCall,
        index: int,
        intent: SideEffect,
    ) -> SideEffectRecord:
        key = call.context.side_effect_keys[index]
        fingerprint = self._intent_fingerprint(call, index, intent)
        record_hash = hashlib.sha256(f"{run_id}:{key}".encode("utf-8")).hexdigest()
        return SideEffectRecord(
            id=f"side_{record_hash[:24]}",
            run_id=run_id,
            idempotency_key=key,
            type=intent.type,
            target=intent.target,
            intent_fingerprint=fingerprint,
            producer_step_id=step_id,
            metadata={
                "program_plan_id": plan_id,
                "tool_name": call.tool_name,
                "risk": intent.risk,
                "intent_metadata": self._json_safe(intent.metadata),
                "side_effect_retry_safe": call.side_effect_retry_safe,
            },
        )

    @staticmethod
    def _intent_fingerprint(
        call: PreparedToolCall,
        index: int,
        intent: SideEffect,
    ) -> str:
        canonical = json.dumps(
            {
                "tool_name": call.tool_name,
                "arguments": call.arguments,
                "index": index,
                "intent": {
                    "type": intent.type,
                    "target": intent.target,
                },
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            default=str,
        ).encode("utf-8")
        return f"sha256:{hashlib.sha256(canonical).hexdigest()}"

    @staticmethod
    def _side_effects_match(
        observed: tuple[SideEffect, ...] | list[SideEffect],
        expected: tuple[SideEffect, ...] | list[SideEffect],
    ) -> bool:
        observed_identities = tuple((item.type, item.target) for item in observed)
        expected_identities = tuple((item.type, item.target) for item in expected)
        return observed_identities == expected_identities

    def _transition(
        self,
        record: SideEffectRecord,
        *,
        expected: SideEffectStatus,
        status: SideEffectStatus,
        metadata: dict[str, Any] | None = None,
    ) -> SideEffectRecord:
        if self._store is None:
            raise RuntimeError("outbox transition requires a durable store")
        return self._store.transition_side_effect(
            record.id,
            expected_status=expected,
            status=status,
            metadata=metadata,
        )

    def _reset_for_idempotent_retry(
        self,
        records: tuple[SideEffectRecord, ...],
    ) -> tuple[SideEffectRecord, ...]:
        return tuple(
            self._transition(
                record,
                expected=SideEffectStatus.DISPATCHING,
                status=SideEffectStatus.PLANNED,
                metadata={"recovery": "idempotent_redispatch"},
            )
            if record.status is SideEffectStatus.DISPATCHING
            else record
            for record in records
        )

    def _reset_failed(
        self,
        records: tuple[SideEffectRecord, ...],
    ) -> tuple[SideEffectRecord, ...]:
        return tuple(
            self._transition(
                record,
                expected=SideEffectStatus.FAILED,
                status=SideEffectStatus.PLANNED,
                metadata={"recovery": "retry_after_definite_failure"},
            )
            if record.status is SideEffectStatus.FAILED
            else record
            for record in records
        )

    def _mark_uncertain(
        self,
        records: tuple[SideEffectRecord, ...],
        *,
        reason: str,
    ) -> tuple[SideEffectRecord, ...]:
        updated: list[SideEffectRecord] = []
        for record in records:
            if record.status is SideEffectStatus.UNCERTAIN:
                updated.append(record)
                continue
            if record.status in {
                SideEffectStatus.DISPATCHING,
                SideEffectStatus.APPLIED,
            }:
                updated.append(
                    self._transition(
                        record,
                        expected=record.status,
                        status=SideEffectStatus.UNCERTAIN,
                        metadata={"uncertain_reason": reason},
                    )
                )
                continue
            updated.append(record)
        return tuple(updated)

    @classmethod
    def _replay_result(
        cls,
        records: tuple[SideEffectRecord, ...],
    ) -> ToolResult | None:
        snapshots = [record.metadata.get("tool_result") for record in records]
        if not snapshots or any(not isinstance(item, dict) for item in snapshots):
            return None
        canonical = {
            json.dumps(item, sort_keys=True, default=str)
            for item in snapshots
        }
        if len(canonical) != 1:
            return None
        first = snapshots[0]
        if not isinstance(first, dict):
            return None
        return cls._result_from_dict(first)

    @classmethod
    def _result_to_dict(cls, result: ToolResult) -> dict[str, Any]:
        return cls._json_safe({
            "ok": result.ok,
            "content": result.content,
            "error": result.error,
            "raw": result.raw,
            "status": result.status.value,
            "error_type": result.error_type.value if result.error_type else None,
            "recoverable_by_model": result.recoverable_by_model,
            "recommended_next_action": (
                result.recommended_next_action.value
                if result.recommended_next_action
                else None
            ),
            "source": result.source,
            "evidence": [item.to_dict() for item in result.evidence],
            "side_effects": [item.to_dict() for item in result.side_effects],
        })

    @staticmethod
    def _result_from_dict(data: dict[str, Any]) -> ToolResult:
        error_type = data.get("error_type")
        recommended = data.get("recommended_next_action")
        return ToolResult(
            ok=bool(data.get("ok")),
            content=str(data.get("content") or ""),
            error=data.get("error"),
            raw=dict(data["raw"]) if isinstance(data.get("raw"), dict) else None,
            status=ToolResultStatus(str(data.get("status") or "error")),
            error_type=ToolErrorType(str(error_type)) if error_type else None,
            recoverable_by_model=bool(data.get("recoverable_by_model")),
            recommended_next_action=(
                RecommendedNextAction(str(recommended))
                if recommended
                else None
            ),
            source=str(data.get("source") or "tool"),
            evidence=tuple(
                Evidence(
                    type=str(item["type"]),
                    ref=str(item["ref"]),
                    summary=str(item.get("summary") or ""),
                    metadata=dict(item.get("metadata") or {}),
                )
                for item in data.get("evidence") or ()
            ),
            side_effects=tuple(
                SideEffect(
                    type=str(item["type"]),
                    target=str(item["target"]),
                    risk=str(item.get("risk") or "low"),
                    metadata=dict(item.get("metadata") or {}),
                )
                for item in data.get("side_effects") or ()
            ),
        )

    @staticmethod
    def _manual_review_result(
        reason: str,
        *,
        evidence: tuple[Evidence, ...] | list[Evidence] = (),
        side_effects: tuple[SideEffect, ...] | list[SideEffect] = (),
    ) -> ToolResult:
        return ToolResult.failure(
            reason,
            error_type=ToolErrorType.CONFLICT,
            recoverable_by_model=False,
            recommended_next_action=RecommendedNextAction.MANUAL_REVIEW,
            evidence=evidence,
            side_effects=side_effects,
            source="side_effect_outbox",
        )

    @staticmethod
    def _json_safe(value: Any) -> Any:
        return json.loads(json.dumps(value, default=str, ensure_ascii=True))


class SideEffectReconciler:
    """Inspect and explicitly resolve uncertain outbox outcomes."""

    def __init__(self, store: LongTaskStore) -> None:
        self._store = store

    def inspect(self, run_id: str) -> OutboxReconciliationReport:
        records = self._store.list_side_effects(run_id)
        return OutboxReconciliationReport(
            run_id=run_id,
            retryable_ids=tuple(
                item.id
                for item in records
                if (
                    item.status
                    in {SideEffectStatus.PLANNED, SideEffectStatus.FAILED}
                    or (
                        item.status is SideEffectStatus.DISPATCHING
                        and bool(item.metadata.get("side_effect_retry_safe"))
                    )
                )
            ),
            pending_result_commit_ids=tuple(
                item.id
                for item in records
                if item.status is SideEffectStatus.APPLIED
            ),
            manual_review_ids=tuple(
                item.id
                for item in records
                if (
                    item.status is SideEffectStatus.UNCERTAIN
                    or (
                        item.status is SideEffectStatus.DISPATCHING
                        and not bool(item.metadata.get("side_effect_retry_safe"))
                    )
                )
            ),
            confirmed_ids=tuple(
                item.id
                for item in records
                if item.status is SideEffectStatus.CONFIRMED
            ),
            reverted_ids=tuple(
                item.id
                for item in records
                if item.status is SideEffectStatus.REVERTED
            ),
        )

    def resolve_uncertain(
        self,
        side_effect_id: str,
        *,
        status: SideEffectStatus,
        note: str,
        result: ToolResult | None = None,
    ) -> SideEffectRecord:
        if status not in {
            SideEffectStatus.CONFIRMED,
            SideEffectStatus.FAILED,
            SideEffectStatus.REVERTED,
        }:
            raise ValueError("uncertain side effect can only resolve to a final verdict")
        if not note.strip():
            raise ValueError("reconciliation note must be non-empty")
        if status is SideEffectStatus.CONFIRMED and result is None:
            raise ValueError(
                "confirmed reconciliation requires a replayable tool result"
            )
        metadata: dict[str, Any] = {
            "reconciliation_note": note,
            "reconciled_manually": True,
        }
        if result is not None:
            metadata["tool_result"] = SideEffectOutbox._result_to_dict(result)
        return self._store.transition_side_effect(
            side_effect_id,
            expected_status=SideEffectStatus.UNCERTAIN,
            status=status,
            metadata=metadata,
        )
