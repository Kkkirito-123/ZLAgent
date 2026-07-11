from __future__ import annotations

import json
import sys
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from re_zlagent.harness.storage import POSTGRES_SCHEMA_SQL, PostgresTaskStore  # noqa: E402
from re_zlagent.harness.tasking import (  # noqa: E402
    AcceptanceCriterion,
    CheckpointStatus,
    CriterionType,
    PlanDAG,
    PlanStep,
    ProgramPlan,
    RunLeaseState,
    TaskContract,
    TaskEventType,
    TaskRun,
    TaskRunStatus,
)
from re_zlagent.harness.tools import Evidence  # noqa: E402


class FakeIntegrityError(Exception):
    pass


class FakeCursor:
    def __init__(self, conn: FakePostgresConnection) -> None:
        self._conn = conn
        self._one: tuple[Any, ...] | None = None
        self._many: list[tuple[Any, ...]] = []

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        normalized = " ".join(sql.lower().split())
        self._conn.executed_sql.append(normalized)
        params = params or ()
        self._one = None
        self._many = []

        if normalized.startswith("create table") or "create table if not exists" in normalized:
            return
        if normalized.startswith("insert into task_contracts"):
            self._conn.contracts[str(params[0])] = str(params[1])
            return
        if normalized == "select payload_json from task_contracts where id = %s":
            payload = self._conn.contracts.get(str(params[0]))
            self._one = (payload,) if payload is not None else None
            return
        if normalized.startswith("insert into task_plans"):
            plan_id = str(params[0])
            contract_id = str(params[1])
            if plan_id in self._conn.plans:
                raise FakeIntegrityError("duplicate plan")
            if contract_id not in self._conn.contracts:
                raise FakeIntegrityError("missing contract")
            self._conn.plans[plan_id] = str(params[2])
            return
        if normalized == "select payload_json from task_plans where id = %s":
            payload = self._conn.plans.get(str(params[0]))
            self._one = (payload,) if payload is not None else None
            return
        if normalized == "select payload_json from task_runs where id = %s":
            payload = self._conn.runs.get(str(params[0]))
            self._one = (payload,) if payload is not None else None
            return
        if normalized == "select payload_json from task_runs where id = %s for update":
            payload = self._conn.runs.get(str(params[0]))
            self._one = (payload,) if payload is not None else None
            return
        if normalized == "select payload_json from task_runs order by id asc":
            self._many = [
                (self._conn.runs[run_id],)
                for run_id in sorted(self._conn.runs)
            ]
            return
        if normalized.startswith("insert into task_runs"):
            run_id = str(params[0])
            contract_id = str(params[1])
            if run_id in self._conn.runs:
                raise FakeIntegrityError("duplicate run")
            if contract_id not in self._conn.contracts:
                raise FakeIntegrityError("missing contract")
            self._conn.runs[run_id] = str(params[5])
            return
        if normalized.startswith("update task_runs set status"):
            run_id = str(params[4])
            payload = json.loads(str(params[3]))
            current = json.loads(self._conn.runs[run_id])
            payload["event_seq"] = max(int(current.get("event_seq") or 0), int(params[2]))
            self._conn.runs[run_id] = json.dumps(payload, sort_keys=True)
            return
        if normalized in {
            "select payload_json from task_run_leases where run_id = %s",
            "select payload_json from task_run_leases where run_id = %s for update",
        }:
            payload = self._conn.leases.get(str(params[0]))
            self._one = (payload,) if payload is not None else None
            return
        if normalized.startswith("insert into task_run_leases"):
            self._conn.leases[str(params[0])] = str(params[3])
            return
        if normalized.startswith("insert into pending_interactions"):
            interaction_id = str(params[0])
            resume_token = str(params[4])
            if interaction_id in self._conn.pending_interactions:
                raise FakeIntegrityError("duplicate interaction")
            if any(
                json.loads(payload)["resume_token"] == resume_token
                for payload in self._conn.pending_interactions.values()
            ):
                raise FakeIntegrityError("duplicate resume token")
            self._conn.pending_interactions[interaction_id] = str(params[5])
            return
        if normalized in {
            "select payload_json from pending_interactions where id = %s",
            "select payload_json from pending_interactions where id = %s for update",
        }:
            payload = self._conn.pending_interactions.get(str(params[0]))
            self._one = (payload,) if payload is not None else None
            return
        if normalized == (
            "select payload_json from pending_interactions "
            "where resume_token = %s"
        ):
            for payload in self._conn.pending_interactions.values():
                if json.loads(payload)["resume_token"] == str(params[0]):
                    self._one = (payload,)
                    return
            return
        if normalized.startswith(
            "select payload_json from pending_interactions where run_id = %s"
        ):
            run_id = str(params[0])
            status = str(params[1]) if len(params) > 1 else None
            rows = [
                payload
                for payload in self._conn.pending_interactions.values()
                if json.loads(payload)["run_id"] == run_id
                and (status is None or json.loads(payload)["status"] == status)
            ]
            self._many = [(payload,) for payload in sorted(rows)]
            return
        if normalized.startswith("update pending_interactions set status"):
            self._conn.pending_interactions[str(params[2])] = str(params[1])
            return
        if normalized.startswith("insert into task_artifacts"):
            artifact_id = str(params[0])
            if artifact_id in self._conn.artifacts:
                raise FakeIntegrityError("duplicate artifact")
            self._conn.artifacts[artifact_id] = str(params[4])
            return
        if normalized == "select payload_json from task_artifacts where id = %s":
            payload = self._conn.artifacts.get(str(params[0]))
            self._one = (payload,) if payload is not None else None
            return
        if normalized.startswith(
            "select payload_json from task_artifacts where run_id = %s"
        ):
            run_id = str(params[0])
            rows = [
                payload
                for payload in self._conn.artifacts.values()
                if json.loads(payload)["run_id"] == run_id
            ]
            self._many = [(payload,) for payload in sorted(rows)]
            return
        if normalized.startswith("insert into side_effect_outbox"):
            side_effect_id = str(params[0])
            run_id = str(params[1])
            key = str(params[2])
            if side_effect_id in self._conn.side_effects:
                raise FakeIntegrityError("duplicate side effect")
            for payload in self._conn.side_effects.values():
                item = json.loads(payload)
                if item["run_id"] == run_id and item["idempotency_key"] == key:
                    raise FakeIntegrityError("duplicate side effect key")
            self._conn.side_effects[side_effect_id] = str(params[4])
            return
        if normalized in {
            "select payload_json from side_effect_outbox where id = %s",
            "select payload_json from side_effect_outbox where id = %s for update",
        }:
            payload = self._conn.side_effects.get(str(params[0]))
            self._one = (payload,) if payload is not None else None
            return
        if normalized == (
            "select payload_json from side_effect_outbox "
            "where run_id = %s and idempotency_key = %s"
        ):
            for payload in self._conn.side_effects.values():
                item = json.loads(payload)
                if (
                    item["run_id"] == str(params[0])
                    and item["idempotency_key"] == str(params[1])
                ):
                    self._one = (payload,)
                    return
            return
        if normalized.startswith(
            "select payload_json from side_effect_outbox where run_id = %s"
        ):
            run_id = str(params[0])
            rows = [
                payload
                for payload in self._conn.side_effects.values()
                if json.loads(payload)["run_id"] == run_id
            ]
            self._many = [(payload,) for payload in sorted(rows)]
            return
        if normalized.startswith("update side_effect_outbox set status"):
            side_effect_id = str(params[2])
            current = json.loads(self._conn.side_effects[side_effect_id])
            if current["status"] != str(params[3]):
                return
            self._conn.side_effects[side_effect_id] = str(params[1])
            return
        if normalized.startswith("select payload_json from task_events where run_id = %s and idempotency_key = %s"):
            run_id = str(params[0])
            key = str(params[1])
            for payload in self._conn.events.values():
                item = json.loads(payload)
                if item["run_id"] == run_id and item.get("idempotency_key") == key:
                    self._one = (payload,)
                    return
            return
        if normalized == "select coalesce(max(seq), 0) from task_events where run_id = %s":
            run_id = str(params[0])
            seqs = [
                int(json.loads(payload)["seq"])
                for payload in self._conn.events.values()
                if json.loads(payload)["run_id"] == run_id
            ]
            self._one = (max(seqs) if seqs else 0,)
            return
        if normalized.startswith("insert into task_events"):
            event_id = str(params[0])
            payload = str(params[5])
            item = json.loads(payload)
            if event_id in self._conn.events:
                raise FakeIntegrityError("duplicate event")
            for existing in self._conn.events.values():
                old = json.loads(existing)
                same_seq = old["run_id"] == item["run_id"] and old["seq"] == item["seq"]
                same_key = (
                    item.get("idempotency_key")
                    and old["run_id"] == item["run_id"]
                    and old.get("idempotency_key") == item.get("idempotency_key")
                )
                if same_seq or same_key:
                    raise FakeIntegrityError("duplicate event")
            self._conn.events[event_id] = payload
            return
        if normalized.startswith("select payload_json from task_events where run_id = %s order by seq asc"):
            run_id = str(params[0])
            rows = [
                payload
                for payload in self._conn.events.values()
                if json.loads(payload)["run_id"] == run_id
            ]
            rows.sort(key=lambda payload: int(json.loads(payload)["seq"]))
            self._many = [(payload,) for payload in rows]
            return
        if normalized == "select coalesce(max(seq), 0) from task_checkpoints where run_id = %s":
            run_id = str(params[0])
            seqs = [
                int(json.loads(payload)["seq"])
                for payload in self._conn.checkpoints.values()
                if json.loads(payload)["run_id"] == run_id
            ]
            self._one = (max(seqs) if seqs else 0,)
            return
        if normalized.startswith("insert into task_checkpoints"):
            checkpoint_id = str(params[0])
            payload = str(params[4])
            if checkpoint_id in self._conn.checkpoints:
                raise FakeIntegrityError("duplicate checkpoint")
            self._conn.checkpoints[checkpoint_id] = payload
            return
        if normalized == "select payload_json from task_checkpoints where id = %s":
            payload = self._conn.checkpoints.get(str(params[0]))
            self._one = (payload,) if payload is not None else None
            return
        if normalized.startswith("select payload_json from task_checkpoints where run_id = %s order by seq desc"):
            rows = self._checkpoint_rows(str(params[0]))
            rows.sort(key=lambda payload: int(json.loads(payload)["seq"]), reverse=True)
            self._one = (rows[0],) if rows else None
            return
        if normalized.startswith("select payload_json from task_checkpoints where run_id = %s order by seq asc"):
            rows = self._checkpoint_rows(str(params[0]))
            rows.sort(key=lambda payload: int(json.loads(payload)["seq"]))
            self._many = [(payload,) for payload in rows]
            return
        raise AssertionError(f"unexpected SQL: {normalized}")

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._one

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._many)

    def close(self) -> None:
        return None

    def _checkpoint_rows(self, run_id: str) -> list[str]:
        return [
            payload
            for payload in self._conn.checkpoints.values()
            if json.loads(payload)["run_id"] == run_id
        ]


class FakePostgresConnection:
    def __init__(self) -> None:
        self.contracts: dict[str, str] = {}
        self.plans: dict[str, str] = {}
        self.runs: dict[str, str] = {}
        self.leases: dict[str, str] = {}
        self.pending_interactions: dict[str, str] = {}
        self.artifacts: dict[str, str] = {}
        self.side_effects: dict[str, str] = {}
        self.events: dict[str, str] = {}
        self.checkpoints: dict[str, str] = {}
        self.executed_sql: list[str] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


class PostgresTaskStoreTests(unittest.TestCase):
    def _contract(self) -> TaskContract:
        return TaskContract(
            id="contract-1",
            user_goal="persist task in postgres",
            acceptance_criteria=(
                AcceptanceCriterion(
                    id="tests",
                    description="tests pass",
                    type=CriterionType.TEST_RESULT,
                ),
            ),
        )

    def _store_with_run(self) -> tuple[PostgresTaskStore, FakePostgresConnection]:
        conn = FakePostgresConnection()
        store = PostgresTaskStore(conn)
        store.save_contract(self._contract())
        store.create_run(TaskRun(id="run-1", contract_id="contract-1"))
        return store, conn

    def _plan(self, *, title: str = "execute") -> ProgramPlan:
        return ProgramPlan(
            id="plan-1",
            contract_id="contract-1",
            dag=PlanDAG((PlanStep(id="step-1", title=title),)),
        )

    def test_schema_preserves_append_only_and_recovery_tables(self) -> None:
        schema = " ".join(POSTGRES_SCHEMA_SQL.lower().split())

        self.assertIn("payload_json jsonb not null", schema)
        self.assertIn("create table if not exists task_plans", schema)
        self.assertIn("create table if not exists task_run_leases", schema)
        self.assertIn("create table if not exists task_events", schema)
        self.assertIn("unique(run_id, seq)", schema)
        self.assertIn("where idempotency_key is not null", schema)
        self.assertIn("create table if not exists task_checkpoints", schema)

    def test_persists_contract_run_event_and_checkpoint(self) -> None:
        store, conn = self._store_with_run()
        event = store.append_event(
            run_id="run-1",
            type=TaskEventType.RUN_CREATED,
            evidence=[Evidence(type="contract", ref="contract-1")],
        )
        checkpoint = store.create_checkpoint(
            run_id="run-1",
            status=CheckpointStatus.RUNNING,
            resume_from_event_id=event.id,
        )

        self.assertEqual(store.get_contract("contract-1").user_goal, "persist task in postgres")
        self.assertEqual(store.get_run("run-1").event_seq, 1)
        self.assertEqual(store.list_events("run-1")[0].evidence[0].ref, "contract-1")
        self.assertEqual(store.latest_checkpoint("run-1").id, checkpoint.id)
        self.assertGreaterEqual(conn.commits, 4)

    def test_contract_save_is_idempotent_but_identity_is_immutable(self) -> None:
        conn = FakePostgresConnection()
        store = PostgresTaskStore(conn)
        contract = self._contract()

        first = store.save_contract(contract)
        replay = store.save_contract(contract)
        store.create_run(TaskRun(id="run-1", contract_id=contract.id))

        self.assertEqual(replay, first)
        with self.assertRaises(ValueError):
            store.save_contract(replace(contract, user_goal="changed goal"))
        self.assertEqual(
            store.get_contract(store.get_run("run-1").contract_id),
            contract,
        )

    def test_plan_roundtrip_is_idempotent_and_immutable(self) -> None:
        conn = FakePostgresConnection()
        store = PostgresTaskStore(conn)
        store.save_contract(self._contract())
        plan = self._plan()

        first = store.save_plan(plan)
        replay = store.save_plan(plan)
        run = store.create_run(
            TaskRun(id="run-1", contract_id="contract-1", plan_id=plan.id)
        )

        self.assertEqual(replay, first)
        self.assertEqual(store.get_plan(plan.id), plan)
        self.assertEqual(run.plan_id, plan.id)
        with self.assertRaises(ValueError):
            store.save_plan(self._plan(title="changed"))

    def test_worker_lease_is_exclusive_reclaimable_and_bounded(self) -> None:
        store, _ = self._store_with_run()
        now = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)

        first = store.claim_run(
            "run-1",
            owner_id="worker-1",
            lease_seconds=10,
            retry_budget=2,
            now=now,
        )
        competing = store.claim_run(
            "run-1",
            owner_id="worker-2",
            lease_seconds=10,
            retry_budget=2,
            now=now + timedelta(seconds=5),
        )
        reclaimed = store.claim_run(
            "run-1",
            owner_id="worker-2",
            lease_seconds=10,
            retry_budget=2,
            now=now + timedelta(seconds=11),
        )
        released = store.release_run_lease(
            "run-1",
            owner_id="worker-2",
            lease_token=reclaimed.lease_token,
            state=RunLeaseState.BACKOFF,
            reason="retry",
            next_attempt_at=now + timedelta(seconds=30),
        )

        self.assertIsNone(competing)
        self.assertEqual(first.attempt_count, 1)
        self.assertEqual(reclaimed.attempt_count, 2)
        self.assertEqual(released.state, RunLeaseState.BACKOFF)
        self.assertEqual(store.get_run_lease("run-1"), released)

    def test_run_status_compare_and_set_rejects_stale_writer(self) -> None:
        store, _ = self._store_with_run()

        updated = store.compare_and_set_run_status(
            "run-1",
            expected_statuses=(TaskRunStatus.CREATED,),
            status=TaskRunStatus.RUNNING,
        )
        stale = store.compare_and_set_run_status(
            "run-1",
            expected_statuses=(TaskRunStatus.CREATED,),
            status=TaskRunStatus.CANCELLED,
        )

        self.assertEqual(updated.status, TaskRunStatus.RUNNING)
        self.assertIsNone(stale)

    def test_run_plan_contract_binding_is_immutable(self) -> None:
        conn = FakePostgresConnection()
        store = PostgresTaskStore(conn)
        contract = self._contract()
        other_contract = replace(contract, id="contract-2")
        store.save_contract(contract)
        store.save_contract(other_contract)
        plan = self._plan()
        other_plan = replace(plan, id="plan-2", contract_id="contract-2")
        store.save_plan(plan)
        store.save_plan(other_plan)

        with self.assertRaises(ValueError):
            store.create_run(
                TaskRun(
                    id="cross-boundary",
                    contract_id="contract-2",
                    plan_id=plan.id,
                )
            )

        run = store.create_run(
            TaskRun(id="run-1", contract_id=contract.id, plan_id=plan.id)
        )
        with self.assertRaises(ValueError):
            store.update_run(replace(run, contract_id="contract-2"))
        with self.assertRaises(ValueError):
            store.update_run(replace(run, plan_id=other_plan.id))

    def test_idempotency_key_returns_existing_event(self) -> None:
        store, _ = self._store_with_run()

        first = store.append_event(
            run_id="run-1",
            type=TaskEventType.TOOL_RESULT_RECORDED,
            payload={"value": 1},
            idempotency_key="tool-1",
        )
        retry = store.append_event(
            run_id="run-1",
            type=TaskEventType.TOOL_RESULT_RECORDED,
            payload={"value": 2},
            idempotency_key="tool-1",
        )

        self.assertEqual(first.id, retry.id)
        self.assertEqual(retry.payload, {"value": 1})
        self.assertEqual(len(store.list_events("run-1")), 1)

    def test_update_run_does_not_decrease_event_seq(self) -> None:
        store, _ = self._store_with_run()
        store.append_event(run_id="run-1", type=TaskEventType.RUN_CREATED)

        updated = store.update_run(
            TaskRun(
                id="run-1",
                contract_id="contract-1",
                status=TaskRunStatus.RUNNING,
                event_seq=0,
            )
        )

        self.assertEqual(updated.event_seq, 1)
        self.assertEqual(store.get_run("run-1").event_seq, 1)

    def test_mutations_lock_run_projection(self) -> None:
        store, conn = self._store_with_run()

        store.append_event(run_id="run-1", type=TaskEventType.RUN_CREATED)
        store.create_checkpoint(run_id="run-1", status=CheckpointStatus.RUNNING)
        store.update_run(
            TaskRun(
                id="run-1",
                contract_id="contract-1",
                status=TaskRunStatus.RUNNING,
            )
        )

        locked_reads = [sql for sql in conn.executed_sql if "for update" in sql]
        self.assertGreaterEqual(len(locked_reads), 3)

    def test_unknown_run_is_rejected_and_rolls_back(self) -> None:
        conn = FakePostgresConnection()
        store = PostgresTaskStore(conn)

        with self.assertRaises(ValueError):
            store.append_event(run_id="missing", type=TaskEventType.RUN_CREATED)

        self.assertEqual(conn.rollbacks, 1)


if __name__ == "__main__":
    unittest.main()
