"""Small SQLite adapter for durable personal memory."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .store import MemoryStoreError, _search_terms
from .types import MemoryEntry, MemoryKind, MemorySource, MemoryWriteResult


class SqliteMemoryStore:
    """SQLite MemoryStore with optimistic versions and bounded compaction."""

    def __init__(
        self,
        path: Path | str,
        *,
        max_entries: int = 200,
        max_entry_chars: int = 500,
    ) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be >= 1")
        if max_entry_chars < 16:
            raise ValueError("max_entry_chars must be >= 16")
        self._path = Path(path)
        self._max_entries = max_entries
        self._max_entry_chars = max_entry_chars
        self._conn = sqlite3.connect(str(self._path))
        self._conn.row_factory = sqlite3.Row
        self._setup()

    def close(self) -> None:
        self._conn.close()

    def add(
        self,
        content: str,
        *,
        kind: MemoryKind = MemoryKind.USER_FACT,
        source: MemorySource = MemorySource.EXPLICIT,
        pinned: bool = False,
        metadata: dict | None = None,
        entry_id: str | None = None,
    ) -> MemoryWriteResult:
        cleaned = (content or "").strip()
        self._validate_content(cleaned)
        self._compact_if_needed()
        entry = MemoryEntry(
            id=entry_id or f"mem_{uuid4().hex}",
            kind=kind,
            content=cleaned,
            source=source,
            pinned=pinned,
            metadata=metadata or {},
        )
        try:
            with self._conn:
                self._conn.execute(
                    """
                    insert into agent_memories (
                        id, kind, content, source, pinned, archived, version,
                        recall_count, created_at, updated_at, last_recalled_at,
                        metadata_json
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    _entry_values(entry),
                )
        except sqlite3.IntegrityError as exc:
            raise MemoryStoreError(f"duplicate memory id: {entry.id}") from exc
        return MemoryWriteResult(entry=entry, created=True)

    def get(self, entry_id: str) -> MemoryEntry | None:
        row = self._conn.execute(
            "select * from agent_memories where id = ?",
            (entry_id,),
        ).fetchone()
        return _entry_from_row(row) if row is not None else None

    def list(
        self,
        *,
        kind: MemoryKind | None = None,
        include_archived: bool = False,
        limit: int | None = None,
    ) -> tuple[MemoryEntry, ...]:
        clauses: list[str] = []
        values: list[Any] = []
        if not include_archived:
            clauses.append("archived = 0")
        if kind is not None:
            clauses.append("kind = ?")
            values.append(kind.value)
        query = "select * from agent_memories"
        if clauses:
            query += " where " + " and ".join(clauses)
        query += " order by pinned desc, updated_at desc, id asc"
        if limit is not None:
            query += " limit ?"
            values.append(max(0, limit))
        rows = self._conn.execute(query, tuple(values)).fetchall()
        return tuple(_entry_from_row(row) for row in rows)

    def search(
        self,
        query: str,
        *,
        limit: int = 5,
        include_archived: bool = False,
    ) -> tuple[MemoryEntry, ...]:
        terms = _search_terms(query)
        if not terms:
            return ()
        scored = [
            (
                entry,
                sum(term in entry.content.casefold() for term in terms),
            )
            for entry in self.list(include_archived=include_archived)
        ]
        scored = [item for item in scored if item[1] > 0]
        scored.sort(
            key=lambda item: (
                not item[0].pinned,
                -item[1],
                -item[0].recall_count,
                -item[0].updated_at.timestamp(),
            )
        )
        return tuple(item[0] for item in scored[: max(1, limit)])

    def update(
        self,
        entry_id: str,
        *,
        observed_version: int,
        content: str | None = None,
        pinned: bool | None = None,
        archived: bool | None = None,
    ) -> MemoryEntry:
        current = self._require_observed(entry_id, observed_version)
        if content is not None:
            self._validate_content((content or "").strip())
        updated = current.with_update(
            content=content,
            pinned=pinned,
            archived=archived,
        )
        self._replace(current, updated)
        return updated

    def touch_recall(self, entry_id: str, *, observed_version: int) -> MemoryEntry:
        current = self._require_observed(entry_id, observed_version)
        updated = current.with_update(recalled=True)
        self._replace(current, updated)
        return updated

    def _replace(self, current: MemoryEntry, updated: MemoryEntry) -> None:
        with self._conn:
            cursor = self._conn.execute(
                """
                update agent_memories
                set kind = ?, content = ?, source = ?, pinned = ?,
                    archived = ?, version = ?, recall_count = ?,
                    created_at = ?, updated_at = ?, last_recalled_at = ?,
                    metadata_json = ?
                where id = ? and version = ?
                """,
                (
                    updated.kind.value,
                    updated.content,
                    updated.source.value,
                    int(updated.pinned),
                    int(updated.archived),
                    updated.version,
                    updated.recall_count,
                    updated.created_at.isoformat(),
                    updated.updated_at.isoformat(),
                    (
                        updated.last_recalled_at.isoformat()
                        if updated.last_recalled_at is not None
                        else None
                    ),
                    json.dumps(updated.metadata, sort_keys=True),
                    updated.id,
                    current.version,
                ),
            )
        if cursor.rowcount != 1:
            raise MemoryStoreError(
                f"stale memory version for {current.id}: observed "
                f"{current.version}"
            )

    def _require_observed(
        self,
        entry_id: str,
        observed_version: int,
    ) -> MemoryEntry:
        entry = self.get(entry_id)
        if entry is None:
            raise MemoryStoreError(f"unknown memory id: {entry_id}")
        if entry.version != observed_version:
            raise MemoryStoreError(
                f"stale memory version for {entry_id}: observed "
                f"{observed_version}, current {entry.version}"
            )
        return entry

    def _validate_content(self, content: str) -> None:
        if not content:
            raise MemoryStoreError("memory content must be non-empty")
        if len(content) > self._max_entry_chars:
            raise MemoryStoreError(
                f"memory content {len(content)} chars exceeds limit "
                f"{self._max_entry_chars}"
            )

    def _compact_if_needed(self) -> None:
        active = self.list()
        if len(active) < self._max_entries:
            return
        candidates = [
            entry
            for entry in active
            if not entry.pinned and entry.kind is not MemoryKind.CONTROL_AXIOM
        ]
        if not candidates:
            raise MemoryStoreError(
                "memory capacity reached; no compactable entries"
            )
        candidates.sort(
            key=lambda item: (item.recall_count, item.updated_at.timestamp())
        )
        victim = candidates[0]
        self.update(
            victim.id,
            observed_version=victim.version,
            archived=True,
        )

    def _setup(self) -> None:
        with self._conn:
            self._conn.execute(
                """
                create table if not exists agent_memories (
                    id text primary key,
                    kind text not null,
                    content text not null,
                    source text not null,
                    pinned integer not null,
                    archived integer not null,
                    version integer not null,
                    recall_count integer not null,
                    created_at text not null,
                    updated_at text not null,
                    last_recalled_at text,
                    metadata_json text not null
                )
                """
            )
            self._conn.execute(
                """
                create index if not exists idx_agent_memories_active_updated
                on agent_memories (archived, pinned, updated_at)
                """
            )


def _entry_values(entry: MemoryEntry) -> tuple[Any, ...]:
    return (
        entry.id,
        entry.kind.value,
        entry.content,
        entry.source.value,
        int(entry.pinned),
        int(entry.archived),
        entry.version,
        entry.recall_count,
        entry.created_at.isoformat(),
        entry.updated_at.isoformat(),
        (
            entry.last_recalled_at.isoformat()
            if entry.last_recalled_at is not None
            else None
        ),
        json.dumps(entry.metadata, sort_keys=True),
    )


def _entry_from_row(row: sqlite3.Row) -> MemoryEntry:
    return MemoryEntry(
        id=str(row["id"]),
        kind=MemoryKind(str(row["kind"])),
        content=str(row["content"]),
        source=MemorySource(str(row["source"])),
        pinned=bool(row["pinned"]),
        archived=bool(row["archived"]),
        version=int(row["version"]),
        recall_count=int(row["recall_count"]),
        created_at=datetime.fromisoformat(str(row["created_at"])),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
        last_recalled_at=(
            datetime.fromisoformat(str(row["last_recalled_at"]))
            if row["last_recalled_at"] is not None
            else None
        ),
        metadata=dict(json.loads(str(row["metadata_json"]))),
    )
