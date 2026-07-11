from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from re_zlagent.harness.storage import (  # noqa: E402
    InMemoryTaskStore,
    SqliteTaskStore,
    TaskStore,
)
from re_zlagent.harness.tasking import (  # noqa: E402
    AcceptanceCriterion,
    CriterionType,
    RunLeaseState,
    TaskContract,
    TaskRun,
    TaskRunStatus,
)


NOW = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)


def build_store(store: TaskStore, *, status: TaskRunStatus) -> TaskStore:
    contract = TaskContract(
        id="contract-1",
        user_goal="execute with one durable owner",
        acceptance_criteria=(
            AcceptanceCriterion(
                id="tests",
                description="tests pass",
                type=CriterionType.TEST_RESULT,
            ),
        ),
    )
    store.save_contract(contract)
    store.create_run(
        TaskRun(
            id="run-1",
            contract_id=contract.id,
            status=status,
        )
    )
    return store


class RunLeaseStoreContract:
    store: TaskStore

    def assert_lease_ownership_and_expiry(self) -> None:
        first = self.store.claim_run(
            "run-1",
            owner_id="worker-1",
            lease_seconds=30,
            retry_budget=3,
            now=NOW,
        )
        competing = self.store.claim_run(
            "run-1",
            owner_id="worker-2",
            lease_seconds=30,
            retry_budget=3,
            now=NOW + timedelta(seconds=10),
        )
        heartbeat = self.store.heartbeat_run_lease(
            "run-1",
            owner_id="worker-1",
            lease_token=first.lease_token,
            lease_seconds=30,
            now=NOW + timedelta(seconds=10),
        )
        reclaimed = self.store.claim_run(
            "run-1",
            owner_id="worker-2",
            lease_seconds=30,
            retry_budget=3,
            now=NOW + timedelta(seconds=41),
        )

        self.assertIsNone(competing)
        self.assertEqual(heartbeat.expires_at, NOW + timedelta(seconds=40))
        self.assertEqual(reclaimed.owner_id, "worker-2")
        self.assertEqual(reclaimed.attempt_count, 2)
        self.assertTrue(reclaimed.metadata["reclaimed_expired_lease"])
        with self.assertRaises(ValueError):
            self.store.heartbeat_run_lease(
                "run-1",
                owner_id="worker-1",
                lease_token=first.lease_token,
                lease_seconds=30,
                now=NOW + timedelta(seconds=42),
            )

    def assert_backoff_and_retry_budget(self) -> None:
        first = self.store.claim_run(
            "run-1",
            owner_id="worker-1",
            lease_seconds=10,
            retry_budget=2,
            now=NOW,
        )
        backoff_until = NOW + timedelta(seconds=20)
        backoff = self.store.release_run_lease(
            "run-1",
            owner_id="worker-1",
            lease_token=first.lease_token,
            state=RunLeaseState.BACKOFF,
            reason="transient failure",
            next_attempt_at=backoff_until,
            last_error="offline",
        )
        too_early = self.store.claim_run(
            "run-1",
            owner_id="worker-2",
            lease_seconds=10,
            retry_budget=2,
            now=NOW + timedelta(seconds=19),
        )
        second = self.store.claim_run(
            "run-1",
            owner_id="worker-2",
            lease_seconds=10,
            retry_budget=2,
            now=backoff_until,
        )
        exhausted = self.store.claim_run(
            "run-1",
            owner_id="worker-3",
            lease_seconds=10,
            retry_budget=2,
            now=backoff_until + timedelta(seconds=11),
        )

        self.assertEqual(backoff.state, RunLeaseState.BACKOFF)
        self.assertIsNone(too_early)
        self.assertEqual(second.attempt_count, 2)
        self.assertIsNone(exhausted)
        self.assertEqual(
            self.store.get_run_lease("run-1").state,
            RunLeaseState.DEAD_LETTER,
        )

    def assert_run_status_compare_and_set(self) -> None:
        updated = self.store.compare_and_set_run_status(
            "run-1",
            expected_statuses=(TaskRunStatus.RUNNING,),
            status=TaskRunStatus.PAUSED,
        )
        stale = self.store.compare_and_set_run_status(
            "run-1",
            expected_statuses=(TaskRunStatus.RUNNING,),
            status=TaskRunStatus.CANCELLED,
        )

        self.assertEqual(updated.status, TaskRunStatus.PAUSED)
        self.assertIsNone(stale)
        self.assertEqual(
            self.store.get_run("run-1").status,
            TaskRunStatus.PAUSED,
        )


class InMemoryRunLeaseStoreTests(unittest.TestCase, RunLeaseStoreContract):
    def setUp(self) -> None:
        self.store = build_store(
            InMemoryTaskStore(),
            status=TaskRunStatus.RUNNING,
        )

    def test_lease_ownership_and_expiry(self) -> None:
        self.assert_lease_ownership_and_expiry()

    def test_backoff_and_retry_budget(self) -> None:
        self.assert_backoff_and_retry_budget()

    def test_run_status_compare_and_set(self) -> None:
        self.assert_run_status_compare_and_set()

    def test_waiting_user_run_cannot_hold_worker_lease(self) -> None:
        store = build_store(
            InMemoryTaskStore(),
            status=TaskRunStatus.WAITING_USER,
        )

        claimed = store.claim_run(
            "run-1",
            owner_id="worker-1",
            lease_seconds=30,
            retry_budget=3,
            now=NOW,
        )

        self.assertIsNone(claimed)
        self.assertIsNone(store.get_run_lease("run-1"))


class SqliteRunLeaseStoreTests(unittest.TestCase, RunLeaseStoreContract):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "leases.sqlite"
        self.store = build_store(
            SqliteTaskStore(self.path),
            status=TaskRunStatus.RUNNING,
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    def test_lease_ownership_and_expiry(self) -> None:
        self.assert_lease_ownership_and_expiry()

    def test_backoff_and_retry_budget(self) -> None:
        self.assert_backoff_and_retry_budget()

    def test_run_status_compare_and_set(self) -> None:
        self.assert_run_status_compare_and_set()

    def test_lease_survives_reopen(self) -> None:
        claimed = self.store.claim_run(
            "run-1",
            owner_id="worker-1",
            lease_seconds=30,
            retry_budget=3,
            now=NOW,
        )
        self.store.close()

        reopened = SqliteTaskStore(self.path)
        loaded = reopened.get_run_lease("run-1")

        self.assertEqual(loaded.lease_token, claimed.lease_token)
        self.assertEqual(loaded.state, RunLeaseState.ACTIVE)
        reopened.close()
        self.store = SqliteTaskStore(self.path)


if __name__ == "__main__":
    unittest.main()
