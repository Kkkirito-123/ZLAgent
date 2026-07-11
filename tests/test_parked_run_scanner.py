from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from re_zlagent.harness.runtime import ParkedRunKind, ParkedRunScanner  # noqa: E402
from re_zlagent.harness.storage import InMemoryLongTaskStore, InMemoryTaskStore  # noqa: E402
from re_zlagent.harness.tasking import (  # noqa: E402
    AcceptanceCriterion,
    CheckpointStatus,
    CriterionType,
    FailureEnvelope,
    FailureType,
    PendingInteraction,
    RecoveryAction,
    RunLeaseState,
    TaskContract,
    TaskRun,
    TaskRunStatus,
)


class ParkedRunScannerTests(unittest.TestCase):
    def _contract(self) -> TaskContract:
        return TaskContract(
            id="contract-1",
            user_goal="scan parked runs",
            acceptance_criteria=(
                AcceptanceCriterion(
                    id="tests",
                    description="tests pass",
                    type=CriterionType.TEST_RESULT,
                ),
            ),
        )

    def _store(self) -> InMemoryTaskStore:
        store = InMemoryTaskStore()
        store.save_contract(self._contract())
        return store

    def test_waiting_user_candidate_includes_resume_token(self) -> None:
        store = self._store()
        store.create_run(
            TaskRun(
                id="run-1",
                contract_id="contract-1",
                status=TaskRunStatus.WAITING_USER,
            )
        )
        checkpoint = store.create_checkpoint(
            run_id="run-1",
            status=CheckpointStatus.WAITING_USER,
            checkpoint_id="chk-1",
        )
        long_task_store = InMemoryLongTaskStore()
        long_task_store.save_pending_interaction(
            PendingInteraction(
                id="pi-1",
                run_id="run-1",
                checkpoint_id=checkpoint.id,
                question="Approve resume?",
                resume_token="resume-token-1",
            )
        )

        candidate = ParkedRunScanner(
            store,
            long_task_store=long_task_store,
        ).inspect("run-1")

        self.assertEqual(candidate.kind, ParkedRunKind.WAITING_USER)
        self.assertFalse(candidate.can_auto_resume)
        self.assertEqual(candidate.pending_interaction_ids, ("pi-1",))
        self.assertEqual(candidate.resume_tokens, ("resume-token-1",))

    def test_recovering_retry_checkpoint_is_auto_resume_candidate(self) -> None:
        store = self._store()
        store.create_run(
            TaskRun(
                id="run-1",
                contract_id="contract-1",
                status=TaskRunStatus.RECOVERING,
            )
        )
        failure = FailureEnvelope(
            status=TaskRunStatus.RECOVERING,
            failed_step="step-1",
            failure_type=FailureType.TOOL_ERROR,
            root_cause="temporary outage",
            recoverable=True,
            recommended_action=RecoveryAction.RETRY,
        )
        store.create_checkpoint(
            run_id="run-1",
            status=CheckpointStatus.FAILED,
            failure=failure,
            checkpoint_id="chk-1",
        )

        scanner = ParkedRunScanner(store)
        candidate = scanner.inspect("run-1")

        self.assertEqual(candidate.kind, ParkedRunKind.AUTO_RETRYABLE)
        self.assertTrue(candidate.can_auto_resume)
        self.assertEqual(scanner.auto_resume_candidates(["run-1"])[0].run_id, "run-1")

    def test_terminal_run_is_not_parked(self) -> None:
        store = self._store()
        store.create_run(
            TaskRun(
                id="run-1",
                contract_id="contract-1",
                status=TaskRunStatus.COMPLETED,
            )
        )

        candidate = ParkedRunScanner(store).inspect("run-1")

        self.assertEqual(candidate.kind, ParkedRunKind.TERMINAL)
        self.assertFalse(candidate.parked)
        self.assertFalse(candidate.can_auto_resume)

    def test_dead_letter_lease_requires_operator(self) -> None:
        store = self._store()
        store.create_run(
            TaskRun(
                id="run-1",
                contract_id="contract-1",
                status=TaskRunStatus.RECOVERING,
            )
        )
        now = datetime(2026, 7, 10, tzinfo=timezone.utc)
        lease = store.claim_run(
            "run-1",
            owner_id="worker-1",
            lease_seconds=30,
            retry_budget=1,
            now=now,
        )
        store.release_run_lease(
            "run-1",
            owner_id="worker-1",
            lease_token=lease.lease_token,
            state=RunLeaseState.DEAD_LETTER,
            reason="retry budget exhausted",
        )

        candidate = ParkedRunScanner(store).inspect("run-1")

        self.assertEqual(candidate.kind, ParkedRunKind.NEEDS_OPERATOR)
        self.assertEqual(candidate.recommended_action, RecoveryAction.MANUAL_REVIEW)
        self.assertEqual(candidate.metadata["lease"]["state"], "dead_letter")

    def test_active_lease_and_backoff_are_not_auto_resumed(self) -> None:
        store = self._store()
        store.create_run(
            TaskRun(
                id="run-1",
                contract_id="contract-1",
                status=TaskRunStatus.RECOVERING,
            )
        )
        failure = FailureEnvelope(
            status=TaskRunStatus.RECOVERING,
            failed_step="step-1",
            failure_type=FailureType.TOOL_ERROR,
            root_cause="temporary outage",
            recoverable=True,
            recommended_action=RecoveryAction.RETRY,
        )
        store.create_checkpoint(
            run_id="run-1",
            status=CheckpointStatus.FAILED,
            failure=failure,
            checkpoint_id="chk-1",
        )
        now = datetime(2026, 7, 10, tzinfo=timezone.utc)
        lease = store.claim_run(
            "run-1",
            owner_id="worker-1",
            lease_seconds=30,
            retry_budget=3,
            now=now,
        )

        owned = ParkedRunScanner(
            store,
            clock=lambda: now + timedelta(seconds=5),
        ).inspect("run-1")
        store.release_run_lease(
            "run-1",
            owner_id="worker-1",
            lease_token=lease.lease_token,
            state=RunLeaseState.BACKOFF,
            reason="retry later",
            next_attempt_at=now + timedelta(seconds=60),
        )
        backing_off = ParkedRunScanner(
            store,
            clock=lambda: now + timedelta(seconds=20),
        ).inspect("run-1")
        ready = ParkedRunScanner(
            store,
            clock=lambda: now + timedelta(seconds=61),
        ).inspect("run-1")

        self.assertEqual(owned.kind, ParkedRunKind.RUNNING)
        self.assertFalse(owned.can_auto_resume)
        self.assertEqual(backing_off.kind, ParkedRunKind.AUTO_RETRYABLE)
        self.assertFalse(backing_off.can_auto_resume)
        self.assertTrue(ready.can_auto_resume)


if __name__ == "__main__":
    unittest.main()
