"""Storage boundary for long-task ledger records."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Protocol

from re_zlagent.harness.tasking import (
    ArtifactRecord,
    InteractionStatus,
    PendingInteraction,
    SideEffectRecord,
    SideEffectStatus,
)


class LongTaskStore(Protocol):
    """Persistence boundary for long-running task ledger records."""

    def save_pending_interaction(
        self,
        interaction: PendingInteraction,
    ) -> PendingInteraction:
        """Persist a pending interaction.

        Implementations should reject duplicate ids and duplicate resume tokens.
        """

    def get_pending_interaction(
        self,
        interaction_id: str,
    ) -> PendingInteraction | None:
        """Return a pending interaction by id."""

    def get_pending_interaction_by_resume_token(
        self,
        resume_token: str,
    ) -> PendingInteraction | None:
        """Return a pending interaction by resume token."""

    def list_pending_interactions(
        self,
        run_id: str,
        *,
        status: InteractionStatus | None = None,
    ) -> tuple[PendingInteraction, ...]:
        """List pending interactions for one run."""

    def resolve_pending_interaction(
        self,
        interaction_id: str,
        answer: str,
    ) -> PendingInteraction:
        """Mark a pending interaction as resolved."""

    def save_artifact(self, artifact: ArtifactRecord) -> ArtifactRecord:
        """Persist an artifact record."""

    def get_artifact(self, artifact_id: str) -> ArtifactRecord | None:
        """Return an artifact record by id."""

    def list_artifacts(self, run_id: str) -> tuple[ArtifactRecord, ...]:
        """List artifact records for one run."""

    def save_side_effect(self, side_effect: SideEffectRecord) -> SideEffectRecord:
        """Persist a side-effect record.

        Reusing the same run-scoped idempotency key should return the existing
        record instead of writing a duplicate.
        """

    def get_side_effect(self, side_effect_id: str) -> SideEffectRecord | None:
        """Return a side-effect record by id."""

    def get_side_effect_by_idempotency_key(
        self,
        run_id: str,
        idempotency_key: str,
    ) -> SideEffectRecord | None:
        """Return a side effect by run-scoped idempotency key."""

    def transition_side_effect(
        self,
        side_effect_id: str,
        *,
        expected_status: SideEffectStatus,
        status: SideEffectStatus,
        metadata: dict[str, Any] | None = None,
    ) -> SideEffectRecord:
        """Atomically transition an outbox record from the expected state."""

    def list_side_effects(self, run_id: str) -> tuple[SideEffectRecord, ...]:
        """List side-effect records for one run."""


class InMemoryLongTaskStore:
    """Reference implementation for long-task ledger persistence."""

    def __init__(self) -> None:
        self._pending: dict[str, PendingInteraction] = {}
        self._pending_by_run: dict[str, list[str]] = {}
        self._pending_by_resume_token: dict[str, str] = {}
        self._artifacts: dict[str, ArtifactRecord] = {}
        self._artifacts_by_run: dict[str, list[str]] = {}
        self._side_effects: dict[str, SideEffectRecord] = {}
        self._side_effects_by_run: dict[str, list[str]] = {}
        self._side_effects_by_idempotency: dict[tuple[str, str], str] = {}

    def save_pending_interaction(
        self,
        interaction: PendingInteraction,
    ) -> PendingInteraction:
        if interaction.id in self._pending:
            raise ValueError(f"duplicate pending interaction id: {interaction.id}")
        if interaction.resume_token in self._pending_by_resume_token:
            raise ValueError(
                f"duplicate pending interaction resume token: {interaction.resume_token}"
            )
        saved = deepcopy(interaction)
        self._pending[interaction.id] = saved
        self._pending_by_run.setdefault(interaction.run_id, []).append(interaction.id)
        self._pending_by_resume_token[interaction.resume_token] = interaction.id
        return deepcopy(saved)

    def get_pending_interaction(
        self,
        interaction_id: str,
    ) -> PendingInteraction | None:
        interaction = self._pending.get(interaction_id)
        return deepcopy(interaction) if interaction is not None else None

    def get_pending_interaction_by_resume_token(
        self,
        resume_token: str,
    ) -> PendingInteraction | None:
        interaction_id = self._pending_by_resume_token.get(resume_token)
        if interaction_id is None:
            return None
        return self.get_pending_interaction(interaction_id)

    def list_pending_interactions(
        self,
        run_id: str,
        *,
        status: InteractionStatus | None = None,
    ) -> tuple[PendingInteraction, ...]:
        items = tuple(
            self._pending[item_id]
            for item_id in self._pending_by_run.get(run_id, ())
        )
        if status is not None:
            items = tuple(item for item in items if item.status is status)
        return deepcopy(items)

    def resolve_pending_interaction(
        self,
        interaction_id: str,
        answer: str,
    ) -> PendingInteraction:
        interaction = self._pending.get(interaction_id)
        if interaction is None:
            raise ValueError(f"unknown pending interaction id: {interaction_id}")
        resolved = interaction.resolve(answer)
        self._pending[interaction_id] = resolved
        return deepcopy(resolved)

    def save_artifact(self, artifact: ArtifactRecord) -> ArtifactRecord:
        if artifact.id in self._artifacts:
            raise ValueError(f"duplicate artifact id: {artifact.id}")
        saved = deepcopy(artifact)
        self._artifacts[artifact.id] = saved
        self._artifacts_by_run.setdefault(artifact.run_id, []).append(artifact.id)
        return deepcopy(saved)

    def get_artifact(self, artifact_id: str) -> ArtifactRecord | None:
        artifact = self._artifacts.get(artifact_id)
        return deepcopy(artifact) if artifact is not None else None

    def list_artifacts(self, run_id: str) -> tuple[ArtifactRecord, ...]:
        return deepcopy(
            tuple(
                self._artifacts[item_id]
                for item_id in self._artifacts_by_run.get(run_id, ())
            )
        )

    def save_side_effect(self, side_effect: SideEffectRecord) -> SideEffectRecord:
        idempotency = (side_effect.run_id, side_effect.idempotency_key)
        existing_id = self._side_effects_by_idempotency.get(idempotency)
        if existing_id is not None:
            existing = self._side_effects[existing_id]
            _ensure_same_side_effect_intent(existing, side_effect)
            return deepcopy(existing)
        if side_effect.id in self._side_effects:
            raise ValueError(f"duplicate side-effect id: {side_effect.id}")
        saved = deepcopy(side_effect)
        self._side_effects[side_effect.id] = saved
        self._side_effects_by_run.setdefault(side_effect.run_id, []).append(
            side_effect.id
        )
        self._side_effects_by_idempotency[idempotency] = side_effect.id
        return deepcopy(saved)

    def get_side_effect(self, side_effect_id: str) -> SideEffectRecord | None:
        side_effect = self._side_effects.get(side_effect_id)
        return deepcopy(side_effect) if side_effect is not None else None

    def get_side_effect_by_idempotency_key(
        self,
        run_id: str,
        idempotency_key: str,
    ) -> SideEffectRecord | None:
        side_effect_id = self._side_effects_by_idempotency.get(
            (run_id, idempotency_key)
        )
        if side_effect_id is None:
            return None
        return self.get_side_effect(side_effect_id)

    def list_side_effects(self, run_id: str) -> tuple[SideEffectRecord, ...]:
        return deepcopy(
            tuple(
                self._side_effects[item_id]
                for item_id in self._side_effects_by_run.get(run_id, ())
            )
        )

    def transition_side_effect(
        self,
        side_effect_id: str,
        *,
        expected_status: SideEffectStatus,
        status: SideEffectStatus,
        metadata: dict[str, Any] | None = None,
    ) -> SideEffectRecord:
        current = self._side_effects.get(side_effect_id)
        if current is None:
            raise ValueError(f"unknown side-effect id: {side_effect_id}")
        if current.status is not expected_status:
            raise ValueError(
                "side-effect status conflict: "
                f"expected {expected_status.value}, found {current.status.value}"
            )
        updated = current.transition(status, metadata=metadata)
        self._side_effects[side_effect_id] = updated
        return deepcopy(updated)


def _ensure_same_side_effect_intent(
    existing: SideEffectRecord,
    candidate: SideEffectRecord,
) -> None:
    same_identity = (
        existing.run_id == candidate.run_id
        and existing.idempotency_key == candidate.idempotency_key
        and existing.type == candidate.type
        and existing.target == candidate.target
        and existing.producer_step_id == candidate.producer_step_id
    )
    fingerprints_match = (
        existing.intent_fingerprint == candidate.intent_fingerprint
    )
    if not same_identity or not fingerprints_match:
        raise ValueError(
            "side-effect idempotency key already exists for a different intent: "
            f"{candidate.idempotency_key}"
        )
