"""SQLite-backed task store.

This adapter is a durable SQL behavior baseline. PostgreSQL adapters should
preserve the same TaskStore semantics.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from re_zlagent.harness.tasking import (
    Checkpoint,
    CheckpointStatus,
    FailureEnvelope,
    ProgramPlan,
    RunLease,
    RunLeaseState,
    TaskContract,
    TaskEvent,
    TaskEventType,
    TaskRun,
    TaskRunStatus,
)
from re_zlagent.harness.tools import Evidence, SideEffect

from .serde import (
    checkpoint_from_dict,
    checkpoint_to_dict,
    contract_from_dict,
    contract_to_dict,
    event_from_dict,
    event_to_dict,
    program_plan_from_dict,
    program_plan_to_dict,
    run_lease_from_dict,
    run_lease_to_dict,
    run_from_dict,
    run_to_dict,
)
from .task_store import (
    ensure_immutable_contract_replay,
    ensure_immutable_plan_replay,
    ensure_immutable_run_binding,
    ensure_plan_matches_run_contract,
)


class SqliteTaskStore:
    """SQLite implementation of TaskStore."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._conn = sqlite3.connect(str(self._path))
        self._conn.row_factory = sqlite3.Row
        self._setup()

    def close(self) -> None:
        self._conn.close()

    def save_contract(self, contract: TaskContract) -> TaskContract:
        payload = json.dumps(contract_to_dict(contract), sort_keys=True)
        try:
            with self._conn:
                self._conn.execute(
                    """
                    insert into task_contracts (id, payload_json)
                    values (?, ?)
                    """,
                    (contract.id, payload),
                )
        except sqlite3.IntegrityError as exc:
            existing = self.get_contract(contract.id)
            if existing is None:
                raise
            try:
                return ensure_immutable_contract_replay(existing, contract)
            except ValueError as conflict:
                raise conflict from exc
        return contract_from_dict(json.loads(payload))

    def get_contract(self, contract_id: str) -> TaskContract | None:
        row = self._conn.execute(
            "select payload_json from task_contracts where id = ?",
            (contract_id,),
        ).fetchone()
        if row is None:
            return None
        return contract_from_dict(json.loads(row["payload_json"]))

    def save_plan(self, plan: ProgramPlan) -> ProgramPlan:
        if self.get_contract(plan.contract_id) is None:
            raise ValueError(f"unknown contract id: {plan.contract_id}")
        payload = json.dumps(program_plan_to_dict(plan), sort_keys=True)
        try:
            with self._conn:
                self._conn.execute(
                    """
                    insert into task_plans (id, contract_id, payload_json)
                    values (?, ?, ?)
                    """,
                    (plan.id, plan.contract_id, payload),
                )
        except sqlite3.IntegrityError as exc:
            existing = self.get_plan(plan.id)
            if existing is None:
                raise
            try:
                return ensure_immutable_plan_replay(existing, plan)
            except ValueError as conflict:
                raise conflict from exc
        return program_plan_from_dict(json.loads(payload))

    def get_plan(self, plan_id: str) -> ProgramPlan | None:
        row = self._conn.execute(
            "select payload_json from task_plans where id = ?",
            (plan_id,),
        ).fetchone()
        if row is None:
            return None
        return program_plan_from_dict(json.loads(row["payload_json"]))

    def create_run(self, run: TaskRun) -> TaskRun:
        if self.get_contract(run.contract_id) is None:
            raise ValueError(f"unknown contract id: {run.contract_id}")
        if run.plan_id is not None:
            plan = self.get_plan(run.plan_id)
            if plan is None:
                raise ValueError(f"unknown plan id: {run.plan_id}")
            ensure_plan_matches_run_contract(run, plan)
        payload = json.dumps(run_to_dict(run), sort_keys=True)
        try:
            with self._conn:
                self._conn.execute(
                    """
                    insert into task_runs
                    (id, contract_id, status, current_checkpoint_id, event_seq, payload_json)
                    values (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run.id,
                        run.contract_id,
                        run.status.value,
                        run.current_checkpoint_id,
                        run.event_seq,
                        payload,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"duplicate run id: {run.id}") from exc
        return run_from_dict(json.loads(payload))

    def get_run(self, run_id: str) -> TaskRun | None:
        row = self._conn.execute(
            "select payload_json from task_runs where id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        return run_from_dict(json.loads(row["payload_json"]))

    def list_runs(
        self,
        *,
        statuses: tuple[TaskRunStatus, ...] | None = None,
        limit: int = 100,
    ) -> tuple[TaskRun, ...]:
        if limit <= 0:
            raise ValueError("limit must be > 0")
        rows = self._conn.execute(
            "select payload_json from task_runs order by rowid asc"
        ).fetchall()
        allowed = set(statuses) if statuses is not None else None
        runs = (
            run_from_dict(json.loads(row["payload_json"]))
            for row in rows
        )
        return tuple(
            run
            for run in runs
            if allowed is None or run.status in allowed
        )[:limit]

    def update_run(self, run: TaskRun) -> TaskRun:
        current = self.get_run(run.id)
        if current is None:
            raise ValueError(f"unknown run id: {run.id}")
        ensure_immutable_run_binding(current, run)
        if self.get_contract(run.contract_id) is None:
            raise ValueError(f"unknown contract id: {run.contract_id}")
        if run.plan_id is not None:
            plan = self.get_plan(run.plan_id)
            if plan is None:
                raise ValueError(f"unknown plan id: {run.plan_id}")
            ensure_plan_matches_run_contract(run, plan)
        normalized = TaskRun(
            id=run.id,
            contract_id=run.contract_id,
            plan_id=run.plan_id,
            status=run.status,
            current_checkpoint_id=run.current_checkpoint_id,
            event_seq=max(run.event_seq, current.event_seq),
            model_name=run.model_name,
            prompt_version=run.prompt_version,
            metadata=run.metadata,
        )
        payload = json.dumps(run_to_dict(normalized), sort_keys=True)
        with self._conn:
            self._conn.execute(
                """
                update task_runs
                set status = ?, current_checkpoint_id = ?, event_seq = ?, payload_json = ?
                where id = ?
                """,
                (
                    normalized.status.value,
                    normalized.current_checkpoint_id,
                    normalized.event_seq,
                    payload,
                    normalized.id,
                ),
            )
        return run_from_dict(json.loads(payload))

    def compare_and_set_run_status(
        self,
        run_id: str,
        *,
        expected_statuses: tuple[TaskRunStatus, ...],
        status: TaskRunStatus,
        checkpoint_id: str | None = None,
    ) -> TaskRun | None:
        if not expected_statuses:
            raise ValueError("expected_statuses must not be empty")
        self._conn.execute("begin immediate")
        try:
            current = self.get_run(run_id)
            if current is None:
                raise ValueError(f"unknown run id: {run_id}")
            if current.status not in set(expected_statuses):
                self._conn.commit()
                return None
            updated = current.with_status(status, checkpoint_id=checkpoint_id)
            self._write_run_projection(updated)
            self._conn.commit()
            return updated
        except Exception:
            self._conn.rollback()
            raise

    def get_run_lease(self, run_id: str) -> RunLease | None:
        self._require_run(run_id)
        return self._get_run_lease_unchecked(run_id)

    def claim_run(
        self,
        run_id: str,
        *,
        owner_id: str,
        lease_seconds: int,
        retry_budget: int,
        now: datetime | None = None,
    ) -> RunLease | None:
        self._conn.execute("begin immediate")
        try:
            run = self.get_run(run_id)
            if run is None:
                raise ValueError(f"unknown run id: {run_id}")
            if run.status not in {
                TaskRunStatus.CREATED,
                TaskRunStatus.RUNNING,
                TaskRunStatus.RECOVERING,
            }:
                self._conn.commit()
                return None
            current = self._get_run_lease_unchecked(run_id) or RunLease(
                run_id=run_id,
                retry_budget=retry_budget,
            )
            if not current.claimable_at(now):
                self._conn.commit()
                return None
            if current.attempt_count >= current.retry_budget:
                self._write_run_lease(
                    current.dead_letter("retry budget exhausted")
                )
                self._conn.commit()
                return None
            claimed = current.claim(
                owner_id=owner_id,
                lease_seconds=lease_seconds,
                now=now,
            )
            self._write_run_lease(claimed)
            self._conn.commit()
            return claimed
        except Exception:
            self._conn.rollback()
            raise

    def heartbeat_run_lease(
        self,
        run_id: str,
        *,
        owner_id: str,
        lease_token: str,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> RunLease:
        self._conn.execute("begin immediate")
        try:
            run = self.get_run(run_id)
            if run is None:
                raise ValueError(f"unknown run id: {run_id}")
            if run.status not in {
                TaskRunStatus.CREATED,
                TaskRunStatus.RUNNING,
                TaskRunStatus.RECOVERING,
            }:
                raise ValueError(f"run is not executable: {run.status.value}")
            current = self._get_run_lease_unchecked(run_id)
            if current is None:
                raise ValueError(f"run has no lease: {run_id}")
            updated = current.heartbeat(
                owner_id=owner_id,
                lease_token=lease_token,
                lease_seconds=lease_seconds,
                now=now,
            )
            self._write_run_lease(updated)
            self._conn.commit()
            return updated
        except Exception:
            self._conn.rollback()
            raise

    def release_run_lease(
        self,
        run_id: str,
        *,
        owner_id: str,
        lease_token: str,
        state: RunLeaseState,
        reason: str,
        next_attempt_at: datetime | None = None,
        last_error: str | None = None,
    ) -> RunLease:
        self._conn.execute("begin immediate")
        try:
            if self.get_run(run_id) is None:
                raise ValueError(f"unknown run id: {run_id}")
            current = self._get_run_lease_unchecked(run_id)
            if current is None:
                raise ValueError(f"run has no lease: {run_id}")
            updated = current.release(
                owner_id=owner_id,
                lease_token=lease_token,
                state=state,
                reason=reason,
                next_attempt_at=next_attempt_at,
                last_error=last_error,
            )
            self._write_run_lease(updated)
            self._conn.commit()
            return updated
        except Exception:
            self._conn.rollback()
            raise

    def append_event(
        self,
        *,
        run_id: str,
        type: TaskEventType,
        payload: dict[str, Any] | None = None,
        evidence: tuple[Evidence, ...] | list[Evidence] = (),
        side_effects: tuple[SideEffect, ...] | list[SideEffect] = (),
        idempotency_key: str | None = None,
        event_id: str | None = None,
        created_at: datetime | None = None,
    ) -> TaskEvent:
        run = self._require_run(run_id)
        if idempotency_key:
            existing = self._event_by_idempotency(run_id, idempotency_key)
            if existing is not None:
                return existing
        seq = self._last_event_seq(run_id) + 1
        event = TaskEvent(
            id=event_id or f"evt_{uuid4().hex}",
            run_id=run_id,
            seq=seq,
            type=type,
            payload=payload or {},
            evidence=tuple(evidence),
            side_effects=tuple(side_effects),
            created_at=created_at or datetime.now().astimezone(),
            idempotency_key=idempotency_key,
        )
        event_payload = json.dumps(event_to_dict(event), sort_keys=True)
        try:
            with self._conn:
                self._conn.execute(
                    """
                    insert into task_events
                    (id, run_id, seq, type, idempotency_key, payload_json)
                    values (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.id,
                        event.run_id,
                        event.seq,
                        event.type.value,
                        event.idempotency_key,
                        event_payload,
                    ),
                )
                updated = run.with_status(run.status, event_seq=max(run.event_seq, seq))
                self._write_run_projection(updated)
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"duplicate task event: {event.id}") from exc
        return event_from_dict(json.loads(event_payload))

    def list_events(self, run_id: str) -> tuple[TaskEvent, ...]:
        self._require_run(run_id)
        rows = self._conn.execute(
            """
            select payload_json from task_events
            where run_id = ?
            order by seq asc
            """,
            (run_id,),
        ).fetchall()
        return tuple(event_from_dict(json.loads(row["payload_json"])) for row in rows)

    def create_checkpoint(
        self,
        *,
        run_id: str,
        status: CheckpointStatus,
        state: dict[str, Any] | None = None,
        resume_from_event_id: str | None = None,
        failure: FailureEnvelope | None = None,
        checkpoint_id: str | None = None,
        created_at: datetime | None = None,
    ) -> Checkpoint:
        run = self._require_run(run_id)
        seq = self._last_checkpoint_seq(run_id) + 1
        checkpoint = Checkpoint(
            id=checkpoint_id or f"chk_{uuid4().hex}",
            run_id=run_id,
            seq=seq,
            status=status,
            state=state or {},
            resume_from_event_id=resume_from_event_id,
            failure=failure,
            created_at=created_at or datetime.now().astimezone(),
        )
        payload = json.dumps(checkpoint_to_dict(checkpoint), sort_keys=True)
        try:
            with self._conn:
                self._conn.execute(
                    """
                    insert into task_checkpoints
                    (id, run_id, seq, status, payload_json)
                    values (?, ?, ?, ?, ?)
                    """,
                    (
                        checkpoint.id,
                        checkpoint.run_id,
                        checkpoint.seq,
                        checkpoint.status.value,
                        payload,
                    ),
                )
                self._write_run_projection(
                    run.with_status(run.status, checkpoint_id=checkpoint.id)
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"duplicate checkpoint id: {checkpoint.id}") from exc
        return checkpoint_from_dict(json.loads(payload))

    def get_checkpoint(self, checkpoint_id: str) -> Checkpoint | None:
        row = self._conn.execute(
            "select payload_json from task_checkpoints where id = ?",
            (checkpoint_id,),
        ).fetchone()
        if row is None:
            return None
        return checkpoint_from_dict(json.loads(row["payload_json"]))

    def latest_checkpoint(self, run_id: str) -> Checkpoint | None:
        self._require_run(run_id)
        row = self._conn.execute(
            """
            select payload_json from task_checkpoints
            where run_id = ?
            order by seq desc
            limit 1
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        return checkpoint_from_dict(json.loads(row["payload_json"]))

    def list_checkpoints(self, run_id: str) -> tuple[Checkpoint, ...]:
        self._require_run(run_id)
        rows = self._conn.execute(
            """
            select payload_json from task_checkpoints
            where run_id = ?
            order by seq asc
            """,
            (run_id,),
        ).fetchall()
        return tuple(checkpoint_from_dict(json.loads(row["payload_json"])) for row in rows)

    def _setup(self) -> None:
        with self._conn:
            self._conn.executescript(
                """
                create table if not exists task_contracts (
                    id text primary key,
                    payload_json text not null
                );
                create table if not exists task_plans (
                    id text primary key,
                    contract_id text not null,
                    payload_json text not null
                );
                create index if not exists task_plans_contract_id_idx
                    on task_plans(contract_id);
                create table if not exists task_runs (
                    id text primary key,
                    contract_id text not null,
                    status text not null,
                    current_checkpoint_id text,
                    event_seq integer not null default 0,
                    payload_json text not null
                );
                create table if not exists task_run_leases (
                    run_id text primary key,
                    state text not null,
                    version integer not null,
                    payload_json text not null
                );
                create index if not exists task_run_leases_state_idx
                    on task_run_leases(state);
                create table if not exists task_events (
                    id text primary key,
                    run_id text not null,
                    seq integer not null,
                    type text not null,
                    idempotency_key text,
                    payload_json text not null,
                    unique(run_id, seq),
                    unique(run_id, idempotency_key)
                );
                create table if not exists task_checkpoints (
                    id text primary key,
                    run_id text not null,
                    seq integer not null,
                    status text not null,
                    payload_json text not null,
                    unique(run_id, seq)
                );
                """
            )

    def _get_run_lease_unchecked(self, run_id: str) -> RunLease | None:
        row = self._conn.execute(
            "select payload_json from task_run_leases where run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        return run_lease_from_dict(json.loads(row["payload_json"]))

    def _write_run_lease(self, lease: RunLease) -> None:
        payload = json.dumps(run_lease_to_dict(lease), sort_keys=True)
        self._conn.execute(
            """
            insert into task_run_leases (run_id, state, version, payload_json)
            values (?, ?, ?, ?)
            on conflict(run_id) do update set
                state = excluded.state,
                version = excluded.version,
                payload_json = excluded.payload_json
            """,
            (lease.run_id, lease.state.value, lease.version, payload),
        )

    def _write_run_projection(self, run: TaskRun) -> None:
        payload = json.dumps(run_to_dict(run), sort_keys=True)
        self._conn.execute(
            """
            update task_runs
            set status = ?, current_checkpoint_id = ?, event_seq = ?, payload_json = ?
            where id = ?
            """,
            (
                run.status.value,
                run.current_checkpoint_id,
                run.event_seq,
                payload,
                run.id,
            ),
        )

    def _require_run(self, run_id: str) -> TaskRun:
        run = self.get_run(run_id)
        if run is None:
            raise ValueError(f"unknown run id: {run_id}")
        return run

    def _event_by_idempotency(
        self,
        run_id: str,
        idempotency_key: str,
    ) -> TaskEvent | None:
        row = self._conn.execute(
            """
            select payload_json from task_events
            where run_id = ? and idempotency_key = ?
            """,
            (run_id, idempotency_key),
        ).fetchone()
        if row is None:
            return None
        return event_from_dict(json.loads(row["payload_json"]))

    def _last_event_seq(self, run_id: str) -> int:
        row = self._conn.execute(
            "select max(seq) as seq from task_events where run_id = ?",
            (run_id,),
        ).fetchone()
        return int(row["seq"] or 0)

    def _last_checkpoint_seq(self, run_id: str) -> int:
        row = self._conn.execute(
            "select max(seq) as seq from task_checkpoints where run_id = ?",
            (run_id,),
        ).fetchone()
        return int(row["seq"] or 0)
