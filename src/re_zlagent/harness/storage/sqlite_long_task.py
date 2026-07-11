"""SQLite-backed long-task ledger store."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

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


class SqliteLongTaskStore:
    """SQLite implementation of LongTaskStore."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._conn = sqlite3.connect(str(self._path))
        self._conn.row_factory = sqlite3.Row
        self._setup()

    def close(self) -> None:
        self._conn.close()

    def save_pending_interaction(
        self,
        interaction: PendingInteraction,
    ) -> PendingInteraction:
        payload = json.dumps(pending_interaction_to_dict(interaction), sort_keys=True)
        try:
            with self._conn:
                self._conn.execute(
                    """
                    insert into pending_interactions
                    (id, run_id, checkpoint_id, status, resume_token, payload_json)
                    values (?, ?, ?, ?, ?, ?)
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
        except sqlite3.IntegrityError as exc:
            if self.get_pending_interaction(interaction.id) is not None:
                raise ValueError(
                    f"duplicate pending interaction id: {interaction.id}"
                ) from exc
            raise ValueError(
                f"duplicate pending interaction resume token: {interaction.resume_token}"
            ) from exc
        return pending_interaction_from_dict(json.loads(payload))

    def get_pending_interaction(
        self,
        interaction_id: str,
    ) -> PendingInteraction | None:
        row = self._conn.execute(
            "select payload_json from pending_interactions where id = ?",
            (interaction_id,),
        ).fetchone()
        if row is None:
            return None
        return pending_interaction_from_dict(json.loads(row["payload_json"]))

    def get_pending_interaction_by_resume_token(
        self,
        resume_token: str,
    ) -> PendingInteraction | None:
        row = self._conn.execute(
            "select payload_json from pending_interactions where resume_token = ?",
            (resume_token,),
        ).fetchone()
        if row is None:
            return None
        return pending_interaction_from_dict(json.loads(row["payload_json"]))

    def list_pending_interactions(
        self,
        run_id: str,
        *,
        status: InteractionStatus | None = None,
    ) -> tuple[PendingInteraction, ...]:
        if status is None:
            rows = self._conn.execute(
                """
                select payload_json from pending_interactions
                where run_id = ?
                order by created_at asc, id asc
                """,
                (run_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                select payload_json from pending_interactions
                where run_id = ? and status = ?
                order by created_at asc, id asc
                """,
                (run_id, status.value),
            ).fetchall()
        return tuple(
            pending_interaction_from_dict(json.loads(row["payload_json"]))
            for row in rows
        )

    def resolve_pending_interaction(
        self,
        interaction_id: str,
        answer: str,
    ) -> PendingInteraction:
        interaction = self.get_pending_interaction(interaction_id)
        if interaction is None:
            raise ValueError(f"unknown pending interaction id: {interaction_id}")
        resolved = interaction.resolve(answer)
        payload = json.dumps(pending_interaction_to_dict(resolved), sort_keys=True)
        with self._conn:
            self._conn.execute(
                """
                update pending_interactions
                set status = ?, payload_json = ?
                where id = ?
                """,
                (resolved.status.value, payload, resolved.id),
            )
        return pending_interaction_from_dict(json.loads(payload))

    def save_artifact(self, artifact: ArtifactRecord) -> ArtifactRecord:
        payload = json.dumps(artifact_to_dict(artifact), sort_keys=True)
        try:
            with self._conn:
                self._conn.execute(
                    """
                    insert into task_artifacts
                    (id, run_id, ref, kind, payload_json)
                    values (?, ?, ?, ?, ?)
                    """,
                    (
                        artifact.id,
                        artifact.run_id,
                        artifact.ref,
                        artifact.kind.value,
                        payload,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"duplicate artifact id: {artifact.id}") from exc
        return artifact_from_dict(json.loads(payload))

    def get_artifact(self, artifact_id: str) -> ArtifactRecord | None:
        row = self._conn.execute(
            "select payload_json from task_artifacts where id = ?",
            (artifact_id,),
        ).fetchone()
        if row is None:
            return None
        return artifact_from_dict(json.loads(row["payload_json"]))

    def list_artifacts(self, run_id: str) -> tuple[ArtifactRecord, ...]:
        rows = self._conn.execute(
            """
            select payload_json from task_artifacts
            where run_id = ?
            order by created_at asc, id asc
            """,
            (run_id,),
        ).fetchall()
        return tuple(artifact_from_dict(json.loads(row["payload_json"])) for row in rows)

    def save_side_effect(self, side_effect: SideEffectRecord) -> SideEffectRecord:
        existing = self.get_side_effect_by_idempotency_key(
            side_effect.run_id,
            side_effect.idempotency_key,
        )
        if existing is not None:
            _ensure_same_side_effect_intent(existing, side_effect)
            return existing
        payload = json.dumps(side_effect_record_to_dict(side_effect), sort_keys=True)
        try:
            with self._conn:
                self._conn.execute(
                    """
                    insert into side_effect_ledger
                    (id, run_id, idempotency_key, status, payload_json)
                    values (?, ?, ?, ?, ?)
                    """,
                    (
                        side_effect.id,
                        side_effect.run_id,
                        side_effect.idempotency_key,
                        side_effect.status.value,
                        payload,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            if self.get_side_effect(side_effect.id) is not None:
                raise ValueError(f"duplicate side-effect id: {side_effect.id}") from exc
            raise ValueError(
                f"duplicate side-effect idempotency key: {side_effect.idempotency_key}"
            ) from exc
        return side_effect_record_from_dict(json.loads(payload))

    def get_side_effect(self, side_effect_id: str) -> SideEffectRecord | None:
        row = self._conn.execute(
            "select payload_json from side_effect_ledger where id = ?",
            (side_effect_id,),
        ).fetchone()
        if row is None:
            return None
        return side_effect_record_from_dict(json.loads(row["payload_json"]))

    def get_side_effect_by_idempotency_key(
        self,
        run_id: str,
        idempotency_key: str,
    ) -> SideEffectRecord | None:
        row = self._conn.execute(
            """
            select payload_json from side_effect_ledger
            where run_id = ? and idempotency_key = ?
            """,
            (run_id, idempotency_key),
        ).fetchone()
        if row is None:
            return None
        return side_effect_record_from_dict(json.loads(row["payload_json"]))

    def list_side_effects(self, run_id: str) -> tuple[SideEffectRecord, ...]:
        rows = self._conn.execute(
            """
            select payload_json from side_effect_ledger
            where run_id = ?
            order by created_at asc, id asc
            """,
            (run_id,),
        ).fetchall()
        return tuple(
            side_effect_record_from_dict(json.loads(row["payload_json"]))
            for row in rows
        )

    def transition_side_effect(
        self,
        side_effect_id: str,
        *,
        expected_status: SideEffectStatus,
        status: SideEffectStatus,
        metadata: dict[str, Any] | None = None,
    ) -> SideEffectRecord:
        current = self.get_side_effect(side_effect_id)
        if current is None:
            raise ValueError(f"unknown side-effect id: {side_effect_id}")
        if current.status is not expected_status:
            raise ValueError(
                "side-effect status conflict: "
                f"expected {expected_status.value}, found {current.status.value}"
            )
        updated = current.transition(status, metadata=metadata)
        payload = json.dumps(side_effect_record_to_dict(updated), sort_keys=True)
        with self._conn:
            cursor = self._conn.execute(
                """
                update side_effect_ledger
                set status = ?, payload_json = ?
                where id = ? and status = ?
                """,
                (
                    updated.status.value,
                    payload,
                    updated.id,
                    expected_status.value,
                ),
            )
        if cursor.rowcount != 1:
            raise ValueError("side-effect status changed concurrently")
        return side_effect_record_from_dict(json.loads(payload))

    def _setup(self) -> None:
        with self._conn:
            self._conn.executescript(
                """
                create table if not exists pending_interactions (
                    id text primary key,
                    run_id text not null,
                    checkpoint_id text not null,
                    status text not null,
                    resume_token text not null unique,
                    payload_json text not null,
                    created_at text not null default current_timestamp
                );
                create index if not exists pending_interactions_run_id_idx
                    on pending_interactions(run_id);
                create index if not exists pending_interactions_run_id_status_idx
                    on pending_interactions(run_id, status);

                create table if not exists task_artifacts (
                    id text primary key,
                    run_id text not null,
                    ref text not null,
                    kind text not null,
                    payload_json text not null,
                    created_at text not null default current_timestamp
                );
                create index if not exists task_artifacts_run_id_idx
                    on task_artifacts(run_id);

                create table if not exists side_effect_ledger (
                    id text primary key,
                    run_id text not null,
                    idempotency_key text not null,
                    status text not null,
                    payload_json text not null,
                    created_at text not null default current_timestamp,
                    unique(run_id, idempotency_key)
                );
                create index if not exists side_effect_ledger_run_id_idx
                    on side_effect_ledger(run_id);
                """
            )
