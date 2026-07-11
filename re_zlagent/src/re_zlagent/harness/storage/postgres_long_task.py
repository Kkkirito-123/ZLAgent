"""PostgreSQL-backed long-task ledger store."""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, Iterator

from re_zlagent.harness.tasking import (
    ArtifactRecord,
    InteractionStatus,
    PendingInteraction,
    SideEffectRecord,
    SideEffectStatus,
)

from .long_task_store import _ensure_same_side_effect_intent
from .serde import (
    artifact_from_dict,
    artifact_to_dict,
    pending_interaction_from_dict,
    pending_interaction_to_dict,
    side_effect_record_from_dict,
    side_effect_record_to_dict,
)


POSTGRES_LONG_TASK_SCHEMA_SQL = """
create table if not exists pending_interactions (
    id text primary key,
    run_id text not null references task_runs(id) on delete cascade,
    checkpoint_id text not null,
    status text not null,
    resume_token text not null unique,
    payload_json jsonb not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists pending_interactions_run_status_idx
    on pending_interactions(run_id, status);

create table if not exists task_artifacts (
    id text primary key,
    run_id text not null references task_runs(id) on delete cascade,
    ref text not null,
    kind text not null,
    payload_json jsonb not null,
    created_at timestamptz not null default now()
);

create index if not exists task_artifacts_run_id_idx
    on task_artifacts(run_id);

create table if not exists side_effect_outbox (
    id text primary key,
    run_id text not null references task_runs(id) on delete cascade,
    idempotency_key text not null,
    status text not null,
    payload_json jsonb not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique(run_id, idempotency_key)
);

create index if not exists side_effect_outbox_run_status_idx
    on side_effect_outbox(run_id, status);
"""


class PostgresLongTaskStore:
    """PostgreSQL implementation of the long-task ledger protocol."""

    def __init__(self, connection: Any, *, setup_schema: bool = True) -> None:
        self._conn = connection
        if setup_schema:
            self.setup_schema()

    @classmethod
    def connect(
        cls,
        dsn: str,
        *,
        setup_schema: bool = True,
        **kwargs: Any,
    ) -> "PostgresLongTaskStore":
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
                cursor.execute(POSTGRES_LONG_TASK_SCHEMA_SQL)

    def save_pending_interaction(
        self,
        interaction: PendingInteraction,
    ) -> PendingInteraction:
        payload = self._dump(pending_interaction_to_dict(interaction))
        try:
            with self._transaction():
                with self._cursor() as cursor:
                    cursor.execute(
                        """
                        insert into pending_interactions
                            (id, run_id, checkpoint_id, status, resume_token, payload_json)
                        values (%s, %s, %s, %s, %s, %s::jsonb)
                        """,
                        (
                            interaction.id,
                            interaction.run_id,
                            interaction.checkpoint_id,
                            interaction.status.value,
                            interaction.resume_token,
                            payload,
                        ),
                    )
        except Exception as exc:
            if not self._is_integrity_error(exc):
                raise
            if self.get_pending_interaction(interaction.id) is not None:
                raise ValueError(
                    f"duplicate pending interaction id: {interaction.id}"
                ) from exc
            raise ValueError(
                "duplicate pending interaction resume token: "
                f"{interaction.resume_token}"
            ) from exc
        return pending_interaction_from_dict(self._load(payload))

    def get_pending_interaction(
        self,
        interaction_id: str,
    ) -> PendingInteraction | None:
        return self._one_record(
            "select payload_json from pending_interactions where id = %s",
            (interaction_id,),
            pending_interaction_from_dict,
        )

    def get_pending_interaction_by_resume_token(
        self,
        resume_token: str,
    ) -> PendingInteraction | None:
        return self._one_record(
            "select payload_json from pending_interactions where resume_token = %s",
            (resume_token,),
            pending_interaction_from_dict,
        )

    def list_pending_interactions(
        self,
        run_id: str,
        *,
        status: InteractionStatus | None = None,
    ) -> tuple[PendingInteraction, ...]:
        if status is None:
            sql = (
                "select payload_json from pending_interactions "
                "where run_id = %s order by created_at asc, id asc"
            )
            params: tuple[Any, ...] = (run_id,)
        else:
            sql = (
                "select payload_json from pending_interactions "
                "where run_id = %s and status = %s "
                "order by created_at asc, id asc"
            )
            params = (run_id, status.value)
        return self._many_records(sql, params, pending_interaction_from_dict)

    def resolve_pending_interaction(
        self,
        interaction_id: str,
        answer: str,
    ) -> PendingInteraction:
        with self._transaction():
            with self._cursor() as cursor:
                cursor.execute(
                    "select payload_json from pending_interactions "
                    "where id = %s for update",
                    (interaction_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise ValueError(
                        f"unknown pending interaction id: {interaction_id}"
                    )
                current = pending_interaction_from_dict(
                    self._load(self._first(row))
                )
                resolved = current.resolve(answer)
                payload = self._dump(pending_interaction_to_dict(resolved))
                cursor.execute(
                    """
                    update pending_interactions
                    set status = %s, payload_json = %s::jsonb, updated_at = now()
                    where id = %s
                    """,
                    (resolved.status.value, payload, resolved.id),
                )
        return pending_interaction_from_dict(self._load(payload))

    def save_artifact(self, artifact: ArtifactRecord) -> ArtifactRecord:
        payload = self._dump(artifact_to_dict(artifact))
        try:
            with self._transaction():
                with self._cursor() as cursor:
                    cursor.execute(
                        """
                        insert into task_artifacts
                            (id, run_id, ref, kind, payload_json)
                        values (%s, %s, %s, %s, %s::jsonb)
                        """,
                        (
                            artifact.id,
                            artifact.run_id,
                            artifact.ref,
                            artifact.kind.value,
                            payload,
                        ),
                    )
        except Exception as exc:
            if self._is_integrity_error(exc):
                raise ValueError(f"duplicate artifact id: {artifact.id}") from exc
            raise
        return artifact_from_dict(self._load(payload))

    def get_artifact(self, artifact_id: str) -> ArtifactRecord | None:
        return self._one_record(
            "select payload_json from task_artifacts where id = %s",
            (artifact_id,),
            artifact_from_dict,
        )

    def list_artifacts(self, run_id: str) -> tuple[ArtifactRecord, ...]:
        return self._many_records(
            "select payload_json from task_artifacts "
            "where run_id = %s order by created_at asc, id asc",
            (run_id,),
            artifact_from_dict,
        )

    def save_side_effect(self, side_effect: SideEffectRecord) -> SideEffectRecord:
        existing = self.get_side_effect_by_idempotency_key(
            side_effect.run_id,
            side_effect.idempotency_key,
        )
        if existing is not None:
            _ensure_same_side_effect_intent(existing, side_effect)
            return existing
        payload = self._dump(side_effect_record_to_dict(side_effect))
        try:
            with self._transaction():
                with self._cursor() as cursor:
                    cursor.execute(
                        """
                        insert into side_effect_outbox
                            (id, run_id, idempotency_key, status, payload_json)
                        values (%s, %s, %s, %s, %s::jsonb)
                        """,
                        (
                            side_effect.id,
                            side_effect.run_id,
                            side_effect.idempotency_key,
                            side_effect.status.value,
                            payload,
                        ),
                    )
        except Exception as exc:
            if not self._is_integrity_error(exc):
                raise
            existing = self.get_side_effect_by_idempotency_key(
                side_effect.run_id,
                side_effect.idempotency_key,
            )
            if existing is not None:
                _ensure_same_side_effect_intent(existing, side_effect)
                return existing
            raise ValueError(f"duplicate side-effect id: {side_effect.id}") from exc
        return side_effect_record_from_dict(self._load(payload))

    def get_side_effect(self, side_effect_id: str) -> SideEffectRecord | None:
        return self._one_record(
            "select payload_json from side_effect_outbox where id = %s",
            (side_effect_id,),
            side_effect_record_from_dict,
        )

    def get_side_effect_by_idempotency_key(
        self,
        run_id: str,
        idempotency_key: str,
    ) -> SideEffectRecord | None:
        return self._one_record(
            "select payload_json from side_effect_outbox "
            "where run_id = %s and idempotency_key = %s",
            (run_id, idempotency_key),
            side_effect_record_from_dict,
        )

    def list_side_effects(self, run_id: str) -> tuple[SideEffectRecord, ...]:
        return self._many_records(
            "select payload_json from side_effect_outbox "
            "where run_id = %s order by created_at asc, id asc",
            (run_id,),
            side_effect_record_from_dict,
        )

    def transition_side_effect(
        self,
        side_effect_id: str,
        *,
        expected_status: SideEffectStatus,
        status: SideEffectStatus,
        metadata: dict[str, Any] | None = None,
    ) -> SideEffectRecord:
        with self._transaction():
            with self._cursor() as cursor:
                cursor.execute(
                    "select payload_json from side_effect_outbox "
                    "where id = %s for update",
                    (side_effect_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise ValueError(f"unknown side-effect id: {side_effect_id}")
                current = side_effect_record_from_dict(
                    self._load(self._first(row))
                )
                if current.status is not expected_status:
                    raise ValueError(
                        "side-effect status conflict: "
                        f"expected {expected_status.value}, "
                        f"found {current.status.value}"
                    )
                updated = current.transition(status, metadata=metadata)
                payload = self._dump(side_effect_record_to_dict(updated))
                cursor.execute(
                    """
                    update side_effect_outbox
                    set status = %s, payload_json = %s::jsonb, updated_at = now()
                    where id = %s and status = %s
                    """,
                    (
                        updated.status.value,
                        payload,
                        updated.id,
                        expected_status.value,
                    ),
                )
        return side_effect_record_from_dict(self._load(payload))

    def _one_record(
        self,
        sql: str,
        params: tuple[Any, ...],
        decoder: Any,
    ) -> Any:
        with self._cursor() as cursor:
            cursor.execute(sql, params)
            row = cursor.fetchone()
        if row is None:
            return None
        return decoder(self._load(self._first(row)))

    def _many_records(
        self,
        sql: str,
        params: tuple[Any, ...],
        decoder: Any,
    ) -> tuple[Any, ...]:
        with self._cursor() as cursor:
            cursor.execute(sql, params)
            rows = cursor.fetchall()
        return tuple(decoder(self._load(self._first(row))) for row in rows)

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
            if callable(close):
                close()

    @staticmethod
    def _dump(payload: dict[str, Any]) -> str:
        return json.dumps(payload, sort_keys=True)

    @staticmethod
    def _load(payload: Any) -> dict[str, Any]:
        if isinstance(payload, str):
            return json.loads(payload)
        return dict(payload)

    @staticmethod
    def _first(row: Any) -> Any:
        if isinstance(row, dict):
            return row.get("payload_json") or next(iter(row.values()))
        return row[0]

    @staticmethod
    def _is_integrity_error(exc: Exception) -> bool:
        name = exc.__class__.__name__.lower()
        return "integrity" in name or "unique" in name
