"""PostgreSQL-backed task store.

This adapter preserves the TaskStore contract: task runs are current
projections, while events and checkpoints are the recoverable source of truth.
It intentionally does not depend on a concrete psycopg package at import time.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator
from uuid import uuid4

from re_zlagent.harness.tasking import (
    Checkpoint,
    CheckpointStatus,
    FailureEnvelope,
    TaskContract,
    TaskEvent,
    TaskEventType,
    TaskRun,
)
from re_zlagent.harness.tools import Evidence, SideEffect

from .serde import (
    checkpoint_from_dict,
    checkpoint_to_dict,
    contract_from_dict,
    contract_to_dict,
    event_from_dict,
    event_to_dict,
    run_from_dict,
    run_to_dict,
)


POSTGRES_SCHEMA_SQL = """
create table if not exists task_contracts (
    id text primary key,
    payload_json jsonb not null,
    updated_at timestamptz not null default now()
);

create table if not exists task_runs (
    id text primary key,
    contract_id text not null references task_contracts(id) on delete restrict,
    status text not null,
    current_checkpoint_id text,
    event_seq integer not null default 0,
    payload_json jsonb not null,
    updated_at timestamptz not null default now()
);

create table if not exists task_events (
    id text primary key,
    run_id text not null references task_runs(id) on delete restrict,
    seq integer not null,
    type text not null,
    idempotency_key text,
    payload_json jsonb not null,
    created_at timestamptz not null default now(),
    unique(run_id, seq)
);

create unique index if not exists task_events_run_id_idempotency_key_idx
    on task_events(run_id, idempotency_key)
    where idempotency_key is not null;

create table if not exists task_checkpoints (
    id text primary key,
    run_id text not null references task_runs(id) on delete restrict,
    seq integer not null,
    status text not null,
    payload_json jsonb not null,
    created_at timestamptz not null default now(),
    unique(run_id, seq)
);
"""


class PostgresTaskStore:
    """PostgreSQL implementation of TaskStore.

    The connection is expected to expose the standard `cursor`, `commit`,
    `rollback`, and `close` methods. This keeps the adapter importable in
    environments where psycopg is not installed yet.
    """

    def __init__(self, connection: Any, *, setup_schema: bool = True) -> None:
        self._conn = connection
        if setup_schema:
            self.setup_schema()

    @classmethod
    def connect(cls, dsn: str, *, setup_schema: bool = True, **kwargs: Any) -> "PostgresTaskStore":
        """Create a store from a psycopg DSN without making psycopg mandatory."""

        try:
            import psycopg  # type: ignore[import-not-found]
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "psycopg is required to connect PostgreSQL. "
                "Install it in the application layer, not in the harness core."
            ) from exc
        return cls(psycopg.connect(dsn, **kwargs), setup_schema=setup_schema)

    def close(self) -> None:
        self._conn.close()

    def setup_schema(self) -> None:
        with self._transaction():
            with self._cursor() as cursor:
                cursor.execute(POSTGRES_SCHEMA_SQL)

    def save_contract(self, contract: TaskContract) -> TaskContract:
        payload = self._dump(contract_to_dict(contract))
        with self._transaction():
            with self._cursor() as cursor:
                cursor.execute(
                    """
                    insert into task_contracts (id, payload_json)
                    values (%s, %s::jsonb)
                    on conflict(id) do update
                    set payload_json = excluded.payload_json,
                        updated_at = now()
                    """,
                    (contract.id, payload),
                )
        return contract_from_dict(self._load(payload))

    def get_contract(self, contract_id: str) -> TaskContract | None:
        with self._cursor() as cursor:
            cursor.execute(
                "select payload_json from task_contracts where id = %s",
                (contract_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return contract_from_dict(self._load(self._first(row)))

    def create_run(self, run: TaskRun) -> TaskRun:
        if self.get_contract(run.contract_id) is None:
            raise ValueError(f"unknown contract id: {run.contract_id}")
        if self.get_run(run.id) is not None:
            raise ValueError(f"duplicate run id: {run.id}")
        payload = self._dump(run_to_dict(run))
        try:
            with self._transaction():
                with self._cursor() as cursor:
                    cursor.execute(
                        """
                        insert into task_runs
                        (id, contract_id, status, current_checkpoint_id, event_seq, payload_json)
                        values (%s, %s, %s, %s, %s, %s::jsonb)
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
        except Exception as exc:
            if self._is_integrity_error(exc):
                raise ValueError(f"duplicate run id: {run.id}") from exc
            raise
        return run_from_dict(self._load(payload))

    def get_run(self, run_id: str) -> TaskRun | None:
        with self._cursor() as cursor:
            cursor.execute("select payload_json from task_runs where id = %s", (run_id,))
            row = cursor.fetchone()
        if row is None:
            return None
        return run_from_dict(self._load(self._first(row)))

    def update_run(self, run: TaskRun) -> TaskRun:
        with self._transaction():
            with self._cursor() as cursor:
                current = self._require_run(cursor, run.id, lock=True)
                if self.get_contract(run.contract_id) is None:
                    raise ValueError(f"unknown contract id: {run.contract_id}")
                normalized = TaskRun(
                    id=run.id,
                    contract_id=run.contract_id,
                    status=run.status,
                    current_checkpoint_id=run.current_checkpoint_id,
                    event_seq=max(run.event_seq, current.event_seq),
                    model_name=run.model_name,
                    prompt_version=run.prompt_version,
                    metadata=run.metadata,
                )
                self._write_run_projection(cursor, normalized)
        return run_from_dict(run_to_dict(normalized))

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
        with self._transaction():
            with self._cursor() as cursor:
                run = self._require_run(cursor, run_id, lock=True)
                if idempotency_key:
                    existing = self._event_by_idempotency(cursor, run_id, idempotency_key)
                    if existing is not None:
                        return existing
                seq = self._last_event_seq(cursor, run_id) + 1
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
                event_payload = self._dump(event_to_dict(event))
                try:
                    cursor.execute(
                        """
                        insert into task_events
                        (id, run_id, seq, type, idempotency_key, payload_json)
                        values (%s, %s, %s, %s, %s, %s::jsonb)
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
                except Exception as exc:
                    if self._is_integrity_error(exc):
                        raise ValueError(f"duplicate task event: {event.id}") from exc
                    raise
                self._write_run_projection(
                    cursor,
                    run.with_status(run.status, event_seq=max(run.event_seq, seq)),
                )
        return event_from_dict(self._load(event_payload))

    def list_events(self, run_id: str) -> tuple[TaskEvent, ...]:
        with self._cursor() as cursor:
            self._require_run(cursor, run_id)
            cursor.execute(
                """
                select payload_json from task_events
                where run_id = %s
                order by seq asc
                """,
                (run_id,),
            )
            rows = cursor.fetchall()
        return tuple(event_from_dict(self._load(self._first(row))) for row in rows)

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
        with self._transaction():
            with self._cursor() as cursor:
                run = self._require_run(cursor, run_id, lock=True)
                seq = self._last_checkpoint_seq(cursor, run_id) + 1
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
                checkpoint_payload = self._dump(checkpoint_to_dict(checkpoint))
                try:
                    cursor.execute(
                        """
                        insert into task_checkpoints
                        (id, run_id, seq, status, payload_json)
                        values (%s, %s, %s, %s, %s::jsonb)
                        """,
                        (
                            checkpoint.id,
                            checkpoint.run_id,
                            checkpoint.seq,
                            checkpoint.status.value,
                            checkpoint_payload,
                        ),
                    )
                except Exception as exc:
                    if self._is_integrity_error(exc):
                        raise ValueError(f"duplicate checkpoint id: {checkpoint.id}") from exc
                    raise
                self._write_run_projection(
                    cursor,
                    run.with_status(run.status, checkpoint_id=checkpoint.id),
                )
        return checkpoint_from_dict(self._load(checkpoint_payload))

    def get_checkpoint(self, checkpoint_id: str) -> Checkpoint | None:
        with self._cursor() as cursor:
            cursor.execute(
                "select payload_json from task_checkpoints where id = %s",
                (checkpoint_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return checkpoint_from_dict(self._load(self._first(row)))

    def latest_checkpoint(self, run_id: str) -> Checkpoint | None:
        with self._cursor() as cursor:
            self._require_run(cursor, run_id)
            cursor.execute(
                """
                select payload_json from task_checkpoints
                where run_id = %s
                order by seq desc
                limit 1
                """,
                (run_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return checkpoint_from_dict(self._load(self._first(row)))

    def list_checkpoints(self, run_id: str) -> tuple[Checkpoint, ...]:
        with self._cursor() as cursor:
            self._require_run(cursor, run_id)
            cursor.execute(
                """
                select payload_json from task_checkpoints
                where run_id = %s
                order by seq asc
                """,
                (run_id,),
            )
            rows = cursor.fetchall()
        return tuple(checkpoint_from_dict(self._load(self._first(row))) for row in rows)

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        try:
            yield
        except Exception:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()

    @contextmanager
    def _cursor(self) -> Iterator[Any]:
        cursor = self._conn.cursor()
        try:
            yield cursor
        finally:
            close = getattr(cursor, "close", None)
            if close is not None:
                close()

    def _require_run(self, cursor: Any, run_id: str, *, lock: bool = False) -> TaskRun:
        cursor.execute(
            "select payload_json from task_runs where id = %s" + (" for update" if lock else ""),
            (run_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise ValueError(f"unknown run id: {run_id}")
        return run_from_dict(self._load(self._first(row)))

    def _event_by_idempotency(
        self,
        cursor: Any,
        run_id: str,
        idempotency_key: str,
    ) -> TaskEvent | None:
        cursor.execute(
            """
            select payload_json from task_events
            where run_id = %s and idempotency_key = %s
            """,
            (run_id, idempotency_key),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return event_from_dict(self._load(self._first(row)))

    def _last_event_seq(self, cursor: Any, run_id: str) -> int:
        cursor.execute(
            "select coalesce(max(seq), 0) from task_events where run_id = %s",
            (run_id,),
        )
        return int(self._first(cursor.fetchone()) or 0)

    def _last_checkpoint_seq(self, cursor: Any, run_id: str) -> int:
        cursor.execute(
            "select coalesce(max(seq), 0) from task_checkpoints where run_id = %s",
            (run_id,),
        )
        return int(self._first(cursor.fetchone()) or 0)

    def _write_run_projection(self, cursor: Any, run: TaskRun) -> None:
        payload = self._dump(run_to_dict(run))
        cursor.execute(
            """
            update task_runs
            set status = %s,
                current_checkpoint_id = %s,
                event_seq = greatest(event_seq, %s),
                payload_json = %s::jsonb,
                updated_at = now()
            where id = %s
            """,
            (
                run.status.value,
                run.current_checkpoint_id,
                run.event_seq,
                payload,
                run.id,
            ),
        )

    def _dump(self, payload: dict[str, Any]) -> str:
        return json.dumps(payload, sort_keys=True)

    def _load(self, payload: Any) -> dict[str, Any]:
        if isinstance(payload, str):
            return json.loads(payload)
        return dict(payload)

    def _first(self, row: Any) -> Any:
        if row is None:
            return None
        if isinstance(row, dict):
            if "payload_json" in row:
                return row["payload_json"]
            if "coalesce" in row:
                return row["coalesce"]
            return next(iter(row.values()))
        return row[0]

    def _is_integrity_error(self, exc: Exception) -> bool:
        name = exc.__class__.__name__.lower()
        return "integrity" in name or "unique" in name
