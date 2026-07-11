from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from re_zlagent.harness.storage import (  # noqa: E402
    POSTGRES_LONG_TASK_SCHEMA_SQL,
    PostgresLongTaskStore,
    PostgresTaskStore,
)
from re_zlagent.harness.tasking import (  # noqa: E402
    AcceptanceCriterion,
    ArtifactKind,
    ArtifactRecord,
    CriterionType,
    InteractionStatus,
    PendingInteraction,
    SideEffectRecord,
    SideEffectStatus,
    TaskContract,
    TaskRun,
)
from tests.test_postgres_task_store import FakePostgresConnection  # noqa: E402


class PostgresLongTaskStoreTests(unittest.TestCase):
    def _stores(
        self,
    ) -> tuple[PostgresTaskStore, PostgresLongTaskStore, FakePostgresConnection]:
        connection = FakePostgresConnection()
        task_store = PostgresTaskStore(connection)
        task_store.save_contract(
            TaskContract(
                id="contract-1",
                user_goal="persist long-task ledgers",
                acceptance_criteria=(
                    AcceptanceCriterion(
                        id="tests",
                        description="tests pass",
                        type=CriterionType.TEST_RESULT,
                    ),
                ),
            )
        )
        task_store.create_run(TaskRun(id="run-1", contract_id="contract-1"))
        return task_store, PostgresLongTaskStore(connection), connection

    def test_schema_contains_all_long_task_ledgers(self) -> None:
        schema = " ".join(POSTGRES_LONG_TASK_SCHEMA_SQL.lower().split())

        self.assertIn("create table if not exists pending_interactions", schema)
        self.assertIn("create table if not exists task_artifacts", schema)
        self.assertIn("create table if not exists side_effect_outbox", schema)
        self.assertIn("unique(run_id, idempotency_key)", schema)

    def test_pending_interaction_and_artifact_roundtrip(self) -> None:
        _, store, _ = self._stores()
        interaction = store.save_pending_interaction(
            PendingInteraction(
                id="interaction-1",
                run_id="run-1",
                checkpoint_id="checkpoint-1",
                question="Approve?",
                resume_token="resume-1",
            )
        )
        store.save_artifact(
            ArtifactRecord(
                id="artifact-1",
                run_id="run-1",
                ref="artifact://result",
                kind=ArtifactKind.OUTPUT,
            )
        )

        resolved = store.resolve_pending_interaction(
            interaction.id,
            "approved",
        )

        self.assertEqual(
            store.get_pending_interaction_by_resume_token("resume-1").id,
            interaction.id,
        )
        self.assertEqual(resolved.status, InteractionStatus.RESOLVED)
        self.assertEqual(
            store.list_pending_interactions(
                "run-1",
                status=InteractionStatus.RESOLVED,
            )[0].answer,
            "approved",
        )
        self.assertEqual(store.list_artifacts("run-1")[0].ref, "artifact://result")

    def test_side_effect_outbox_is_idempotent_and_compare_and_set(self) -> None:
        _, store, _ = self._stores()
        planned = SideEffectRecord(
            id="side-1",
            run_id="run-1",
            idempotency_key="run:1:step:1",
            type="message",
            target="user-1",
            intent_fingerprint="sha256:intent-1",
        )

        first = store.save_side_effect(planned)
        replay = store.save_side_effect(planned)
        dispatching = store.transition_side_effect(
            first.id,
            expected_status=SideEffectStatus.PLANNED,
            status=SideEffectStatus.DISPATCHING,
        )
        applied = store.transition_side_effect(
            first.id,
            expected_status=SideEffectStatus.DISPATCHING,
            status=SideEffectStatus.APPLIED,
            metadata={"external_ref": "message-1"},
        )

        self.assertEqual(first, replay)
        self.assertEqual(dispatching.attempt_count, 1)
        self.assertEqual(applied.metadata["external_ref"], "message-1")
        self.assertEqual(store.list_side_effects("run-1"), (applied,))
        with self.assertRaises(ValueError):
            store.transition_side_effect(
                first.id,
                expected_status=SideEffectStatus.DISPATCHING,
                status=SideEffectStatus.APPLIED,
            )
        with self.assertRaises(ValueError):
            store.save_side_effect(
                SideEffectRecord(
                    id="side-2",
                    run_id="run-1",
                    idempotency_key="run:1:step:1",
                    type="message",
                    target="user-2",
                    intent_fingerprint="sha256:intent-2",
                )
            )


if __name__ == "__main__":
    unittest.main()
