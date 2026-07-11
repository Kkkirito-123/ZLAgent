from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from re_zlagent.harness.storage import InMemoryLongTaskStore  # noqa: E402
from re_zlagent.harness.tasking import (  # noqa: E402
    ArtifactKind,
    ArtifactRecord,
    InteractionStatus,
    PendingInteraction,
    SideEffectRecord,
    SideEffectStatus,
)


class InMemoryLongTaskStoreTests(unittest.TestCase):
    def test_pending_interaction_roundtrip_and_resolution(self) -> None:
        store = InMemoryLongTaskStore()
        interaction = PendingInteraction(
            id="pi-1",
            run_id="run-1",
            checkpoint_id="chk-1",
            question="Approve resume?",
            required_by_step_id="step-1",
            resume_token="resume-token-1",
        )

        saved = store.save_pending_interaction(interaction)
        loaded = store.get_pending_interaction("pi-1")
        by_token = store.get_pending_interaction_by_resume_token("resume-token-1")

        self.assertEqual(saved.id, "pi-1")
        self.assertEqual(loaded.question, "Approve resume?")
        self.assertEqual(by_token.id, "pi-1")
        self.assertEqual(
            store.list_pending_interactions(
                "run-1",
                status=InteractionStatus.OPEN,
            )[0].id,
            "pi-1",
        )

        resolved = store.resolve_pending_interaction(
            "pi-1",
            "Approved",
        )

        self.assertEqual(resolved.status, InteractionStatus.RESOLVED)
        self.assertEqual(resolved.answer, "Approved")
        self.assertEqual(
            store.list_pending_interactions(
                "run-1",
                status=InteractionStatus.OPEN,
            ),
            (),
        )

    def test_pending_interaction_rejects_duplicate_resume_token(self) -> None:
        store = InMemoryLongTaskStore()
        store.save_pending_interaction(
            PendingInteraction(
                id="pi-1",
                run_id="run-1",
                checkpoint_id="chk-1",
                question="Approve?",
                resume_token="resume-token-1",
            )
        )

        with self.assertRaises(ValueError):
            store.save_pending_interaction(
                PendingInteraction(
                    id="pi-2",
                    run_id="run-1",
                    checkpoint_id="chk-1",
                    question="Approve again?",
                    resume_token="resume-token-1",
                )
            )

    def test_artifact_records_are_listed_by_run(self) -> None:
        store = InMemoryLongTaskStore()
        artifact = ArtifactRecord(
            id="artifact-1",
            run_id="run-1",
            ref="artifact://report",
            kind=ArtifactKind.REPORT,
            producer_step_id="step-1",
            evidence_refs=("evidence:report",),
        )

        saved = store.save_artifact(artifact)

        self.assertEqual(saved.ref, "artifact://report")
        self.assertEqual(store.get_artifact("artifact-1").kind, ArtifactKind.REPORT)
        self.assertEqual(store.list_artifacts("run-1")[0].id, "artifact-1")

    def test_side_effect_records_are_idempotent_by_run_key(self) -> None:
        store = InMemoryLongTaskStore()
        first = SideEffectRecord(
            id="side-effect-1",
            run_id="run-1",
            idempotency_key="send:message:1",
            type="message",
            target="user-1",
            status=SideEffectStatus.APPLIED,
        )
        retry = SideEffectRecord(
            id="side-effect-2",
            run_id="run-1",
            idempotency_key="send:message:1",
            type="message",
            target="user-1",
            status=SideEffectStatus.PLANNED,
        )

        saved = store.save_side_effect(first)
        repeated = store.save_side_effect(retry)

        self.assertEqual(saved.id, "side-effect-1")
        self.assertEqual(repeated.id, "side-effect-1")
        self.assertEqual(
            store.get_side_effect_by_idempotency_key(
                "run-1",
                "send:message:1",
            ).status,
            SideEffectStatus.APPLIED,
        )
        self.assertEqual(len(store.list_side_effects("run-1")), 1)

    def test_side_effect_outbox_transitions_use_compare_and_set(self) -> None:
        store = InMemoryLongTaskStore()
        planned = store.save_side_effect(
            SideEffectRecord(
                id="side-effect-1",
                run_id="run-1",
                idempotency_key="send:1",
                type="message",
                target="user-1",
                intent_fingerprint="sha256:intent-1",
            )
        )

        dispatching = store.transition_side_effect(
            planned.id,
            expected_status=SideEffectStatus.PLANNED,
            status=SideEffectStatus.DISPATCHING,
        )
        applied = store.transition_side_effect(
            planned.id,
            expected_status=SideEffectStatus.DISPATCHING,
            status=SideEffectStatus.APPLIED,
            metadata={"external_ref": "message-1"},
        )
        confirmed = store.transition_side_effect(
            planned.id,
            expected_status=SideEffectStatus.APPLIED,
            status=SideEffectStatus.CONFIRMED,
        )

        self.assertEqual(dispatching.attempt_count, 1)
        self.assertEqual(applied.metadata["external_ref"], "message-1")
        self.assertEqual(confirmed.status, SideEffectStatus.CONFIRMED)
        with self.assertRaises(ValueError):
            store.transition_side_effect(
                planned.id,
                expected_status=SideEffectStatus.APPLIED,
                status=SideEffectStatus.CONFIRMED,
            )
        with self.assertRaises(ValueError):
            confirmed.transition(SideEffectStatus.DISPATCHING)

    def test_idempotency_key_rejects_a_different_side_effect_intent(self) -> None:
        store = InMemoryLongTaskStore()
        store.save_side_effect(
            SideEffectRecord(
                id="side-effect-1",
                run_id="run-1",
                idempotency_key="send:1",
                type="message",
                target="user-1",
                intent_fingerprint="sha256:intent-1",
            )
        )

        with self.assertRaises(ValueError):
            store.save_side_effect(
                SideEffectRecord(
                    id="side-effect-2",
                    run_id="run-1",
                    idempotency_key="send:1",
                    type="message",
                    target="user-2",
                    intent_fingerprint="sha256:intent-2",
                )
            )


if __name__ == "__main__":
    unittest.main()
