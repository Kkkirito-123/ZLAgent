from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from re_zlagent.harness.storage import SqliteLongTaskStore  # noqa: E402
from re_zlagent.harness.tasking import (  # noqa: E402
    ArtifactKind,
    ArtifactRecord,
    InteractionStatus,
    PendingInteraction,
    SideEffectRecord,
    SideEffectStatus,
)


class SqliteLongTaskStoreTests(unittest.TestCase):
    def test_persists_pending_interaction_and_artifact_across_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "long_task.sqlite"
            store = SqliteLongTaskStore(db_path)
            store.save_pending_interaction(
                PendingInteraction(
                    id="pi-1",
                    run_id="run-1",
                    checkpoint_id="chk-1",
                    question="Approve resume?",
                    required_by_step_id="step-1",
                    resume_token="resume-token-1",
                )
            )
            store.save_artifact(
                ArtifactRecord(
                    id="artifact-1",
                    run_id="run-1",
                    ref="artifact://summary",
                    kind=ArtifactKind.REPORT,
                    producer_step_id="step-1",
                    evidence_refs=("evidence:summary",),
                )
            )
            store.close()

            reopened = SqliteLongTaskStore(db_path)
            self.assertEqual(
                reopened.get_pending_interaction_by_resume_token(
                    "resume-token-1",
                ).id,
                "pi-1",
            )
            self.assertEqual(
                reopened.list_pending_interactions(
                    "run-1",
                    status=InteractionStatus.OPEN,
                )[0].required_by_step_id,
                "step-1",
            )
            self.assertEqual(reopened.list_artifacts("run-1")[0].ref, "artifact://summary")
            reopened.close()

    def test_resolve_pending_interaction_updates_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SqliteLongTaskStore(Path(tmp) / "long_task.sqlite")
            store.save_pending_interaction(
                PendingInteraction(
                    id="pi-1",
                    run_id="run-1",
                    checkpoint_id="chk-1",
                    question="Approve resume?",
                    resume_token="resume-token-1",
                )
            )

            resolved = store.resolve_pending_interaction("pi-1", "Approved")

            self.assertEqual(resolved.status, InteractionStatus.RESOLVED)
            self.assertEqual(store.list_pending_interactions("run-1", status=InteractionStatus.OPEN), ())
            self.assertEqual(
                store.list_pending_interactions(
                    "run-1",
                    status=InteractionStatus.RESOLVED,
                )[0].answer,
                "Approved",
            )
            store.close()

    def test_side_effect_is_idempotent_across_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "long_task.sqlite"
            store = SqliteLongTaskStore(db_path)
            store.save_side_effect(
                SideEffectRecord(
                    id="side-effect-1",
                    run_id="run-1",
                    idempotency_key="send:1",
                    type="message",
                    target="user-1",
                    status=SideEffectStatus.APPLIED,
                )
            )
            store.close()

            reopened = SqliteLongTaskStore(db_path)
            repeated = reopened.save_side_effect(
                SideEffectRecord(
                    id="side-effect-2",
                    run_id="run-1",
                    idempotency_key="send:1",
                    type="message",
                    target="user-1",
                    status=SideEffectStatus.PLANNED,
                )
            )

            self.assertEqual(repeated.id, "side-effect-1")
            self.assertEqual(repeated.status, SideEffectStatus.APPLIED)
            self.assertEqual(len(reopened.list_side_effects("run-1")), 1)
            reopened.close()

    def test_side_effect_transition_survives_reopen_and_checks_expected_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "long_task.sqlite"
            store = SqliteLongTaskStore(db_path)
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
            store.transition_side_effect(
                planned.id,
                expected_status=SideEffectStatus.PLANNED,
                status=SideEffectStatus.DISPATCHING,
            )
            store.close()

            reopened = SqliteLongTaskStore(db_path)
            applied = reopened.transition_side_effect(
                planned.id,
                expected_status=SideEffectStatus.DISPATCHING,
                status=SideEffectStatus.APPLIED,
                metadata={"external_ref": "message-1"},
            )

            self.assertEqual(applied.attempt_count, 1)
            self.assertEqual(applied.metadata["external_ref"], "message-1")
            with self.assertRaises(ValueError):
                reopened.transition_side_effect(
                    planned.id,
                    expected_status=SideEffectStatus.DISPATCHING,
                    status=SideEffectStatus.APPLIED,
                )
            reopened.close()


if __name__ == "__main__":
    unittest.main()
