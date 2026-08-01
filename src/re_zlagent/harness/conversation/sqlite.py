"""SQLite adapter for durable conversation sessions."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from .store import ConversationStoreError
from .types import (
    CompactionReason,
    ConversationCompaction,
    ConversationTurn,
    _validate_session_id,
)


class SqliteConversationStore:
    """Durable raw-turn and append-only compaction storage."""

    def __init__(
        self,
        path: Path | str,
        *,
        raw_retention_rounds: int = 32,
    ) -> None:
        if raw_retention_rounds < 1:
            raise ValueError("raw_retention_rounds must be >= 1")
        self._path = Path(path)
        self._raw_retention_rounds = raw_retention_rounds
        self._conn = sqlite3.connect(str(self._path))
        self._conn.row_factory = sqlite3.Row
        self._setup()

    def close(self) -> None:
        self._conn.close()

    def append_turn(
        self,
        session_id: str,
        user_content: str,
        assistant_content: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> ConversationTurn:
        _validate_session_id(session_id)
        payload = json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)
        with self._conn:
            row = self._conn.execute(
                "select coalesce(max(sequence), 0) as sequence "
                "from conversation_turns where session_id = ?",
                (session_id,),
            ).fetchone()
            sequence = int(row["sequence"]) + 1
            turn = ConversationTurn(
                session_id=session_id,
                sequence=sequence,
                user_content=user_content,
                assistant_content=assistant_content,
                metadata=metadata or {},
            )
            try:
                self._conn.execute(
                    """
                    insert into conversation_turns (
                        session_id, sequence, user_content, assistant_content,
                        archived, created_at, metadata_json
                    ) values (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        sequence,
                        turn.user_content,
                        turn.assistant_content,
                        0,
                        turn.created_at.isoformat(),
                        payload,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConversationStoreError(
                    "conversation turn sequence conflict"
                ) from exc
            hot_boundary = max(
                1,
                sequence - self._raw_retention_rounds + 1,
            )
            self._conn.execute(
                """
                update conversation_turns
                set archived = case when sequence < ? then 1 else 0 end
                where session_id = ?
                """,
                (hot_boundary, session_id),
            )
        return turn

    def list_turns(
        self,
        session_id: str,
        *,
        after_sequence: int = 0,
        include_archived: bool = False,
    ) -> tuple[ConversationTurn, ...]:
        _validate_session_id(session_id)
        if after_sequence < 0:
            raise ValueError("after_sequence must be >= 0")
        query = (
            "select * from conversation_turns "
            "where session_id = ? and sequence > ?"
        )
        values: list[Any] = [session_id, after_sequence]
        if not include_archived:
            query += " and archived = 0"
        query += " order by sequence asc"
        rows = self._conn.execute(query, tuple(values)).fetchall()
        return tuple(_turn_from_row(row) for row in rows)

    def latest_compaction(
        self,
        session_id: str,
    ) -> ConversationCompaction | None:
        _validate_session_id(session_id)
        row = self._conn.execute(
            """
            select * from conversation_compactions
            where session_id = ?
            order by summarized_through_sequence desc
            limit 1
            """,
            (session_id,),
        ).fetchone()
        return _compaction_from_row(row) if row is not None else None

    def append_compaction(
        self,
        compaction: ConversationCompaction,
    ) -> ConversationCompaction:
        with self._conn:
            existing = self._conn.execute(
                "select * from conversation_compactions where id = ?",
                (compaction.id,),
            ).fetchone()
            if existing is not None:
                stored = _compaction_from_row(existing)
                if stored == compaction:
                    return stored
                raise ConversationStoreError(
                    f"conflicting conversation compaction id: {compaction.id}"
                )
            latest = self._conn.execute(
                """
                select summarized_through_sequence
                from conversation_compactions
                where session_id = ?
                order by summarized_through_sequence desc
                limit 1
                """,
                (compaction.session_id,),
            ).fetchone()
            if (
                latest is not None
                and compaction.summarized_through_sequence
                <= int(latest["summarized_through_sequence"])
            ):
                raise ConversationStoreError(
                    "conversation compaction must advance the summarized prefix"
                )
            try:
                self._conn.execute(
                    """
                    insert into conversation_compactions (
                        id, session_id, summarized_through_sequence, summary,
                        reason, source_turn_count, estimated_input_tokens,
                        estimated_output_tokens, created_at, metadata_json
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        compaction.id,
                        compaction.session_id,
                        compaction.summarized_through_sequence,
                        compaction.summary,
                        compaction.reason.value,
                        compaction.source_turn_count,
                        compaction.estimated_input_tokens,
                        compaction.estimated_output_tokens,
                        compaction.created_at.isoformat(),
                        json.dumps(
                            compaction.metadata,
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConversationStoreError(
                    "conversation compaction conflict"
                ) from exc
        return compaction

    def _setup(self) -> None:
        with self._conn:
            self._conn.execute(
                """
                create table if not exists conversation_turns (
                    session_id text not null,
                    sequence integer not null,
                    user_content text not null,
                    assistant_content text not null,
                    archived integer not null,
                    created_at text not null,
                    metadata_json text not null,
                    primary key (session_id, sequence)
                )
                """
            )
            self._conn.execute(
                """
                create index if not exists idx_conversation_turns_hot
                on conversation_turns (session_id, archived, sequence)
                """
            )
            self._conn.execute(
                """
                create table if not exists conversation_compactions (
                    id text primary key,
                    session_id text not null,
                    summarized_through_sequence integer not null,
                    summary text not null,
                    reason text not null,
                    source_turn_count integer not null,
                    estimated_input_tokens integer not null,
                    estimated_output_tokens integer not null,
                    created_at text not null,
                    metadata_json text not null,
                    unique (session_id, summarized_through_sequence)
                )
                """
            )


def _turn_from_row(row: sqlite3.Row) -> ConversationTurn:
    return ConversationTurn(
        session_id=str(row["session_id"]),
        sequence=int(row["sequence"]),
        user_content=str(row["user_content"]),
        assistant_content=str(row["assistant_content"]),
        archived=bool(row["archived"]),
        created_at=datetime.fromisoformat(str(row["created_at"])),
        metadata=dict(json.loads(str(row["metadata_json"]))),
    )


def _compaction_from_row(row: sqlite3.Row) -> ConversationCompaction:
    return ConversationCompaction(
        id=str(row["id"]),
        session_id=str(row["session_id"]),
        summarized_through_sequence=int(row["summarized_through_sequence"]),
        summary=str(row["summary"]),
        reason=CompactionReason(str(row["reason"])),
        source_turn_count=int(row["source_turn_count"]),
        estimated_input_tokens=int(row["estimated_input_tokens"]),
        estimated_output_tokens=int(row["estimated_output_tokens"]),
        created_at=datetime.fromisoformat(str(row["created_at"])),
        metadata=dict(json.loads(str(row["metadata_json"]))),
    )
