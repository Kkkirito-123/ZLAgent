"""Memory store protocol and in-memory behavior baseline."""

from __future__ import annotations

from copy import deepcopy
from typing import Protocol
from uuid import uuid4

from .types import MemoryEntry, MemoryKind, MemorySource, MemoryWriteResult


class MemoryStoreError(Exception):
    """Caller-correctable memory store error."""


class MemoryStore(Protocol):
    """Persistence boundary for durable memory entries."""

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
        """Create a memory entry."""

    def get(self, entry_id: str) -> MemoryEntry | None:
        """Return a memory entry by id."""

    def list(
        self,
        *,
        kind: MemoryKind | None = None,
        include_archived: bool = False,
        limit: int | None = None,
    ) -> tuple[MemoryEntry, ...]:
        """Return entries in prompt-friendly order."""

    def search(
        self,
        query: str,
        *,
        limit: int = 5,
        include_archived: bool = False,
    ) -> tuple[MemoryEntry, ...]:
        """Search entries."""

    def update(
        self,
        entry_id: str,
        *,
        observed_version: int,
        content: str | None = None,
        pinned: bool | None = None,
        archived: bool | None = None,
    ) -> MemoryEntry:
        """Update an entry after observing its current version."""

    def touch_recall(self, entry_id: str, *, observed_version: int) -> MemoryEntry:
        """Record that an entry was recalled."""


class InMemoryMemoryStore:
    """Reference in-memory memory store."""

    def __init__(self, *, max_entries: int = 200, max_entry_chars: int = 500) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be >= 1")
        if max_entry_chars < 16:
            raise ValueError("max_entry_chars must be >= 16")
        self._max_entries = max_entries
        self._max_entry_chars = max_entry_chars
        self._entries: dict[str, MemoryEntry] = {}

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
        memory_id = entry_id or f"mem_{uuid4().hex}"
        if memory_id in self._entries:
            raise MemoryStoreError(f"duplicate memory id: {memory_id}")
        entry = MemoryEntry(
            id=memory_id,
            kind=kind,
            content=cleaned,
            source=source,
            pinned=pinned,
            metadata=metadata or {},
        )
        self._entries[memory_id] = deepcopy(entry)
        return MemoryWriteResult(entry=deepcopy(entry), created=True)

    def get(self, entry_id: str) -> MemoryEntry | None:
        entry = self._entries.get(entry_id)
        return deepcopy(entry) if entry is not None else None

    def list(
        self,
        *,
        kind: MemoryKind | None = None,
        include_archived: bool = False,
        limit: int | None = None,
    ) -> tuple[MemoryEntry, ...]:
        entries = [
            item
            for item in self._entries.values()
            if (include_archived or not item.archived)
            and (kind is None or item.kind is kind)
        ]
        entries.sort(
            key=lambda item: (
                not item.pinned,
                -item.updated_at.timestamp(),
                item.id,
            )
        )
        if limit is not None:
            entries = entries[: max(0, limit)]
        return deepcopy(tuple(entries))

    def search(
        self,
        query: str,
        *,
        limit: int = 5,
        include_archived: bool = False,
    ) -> tuple[MemoryEntry, ...]:
        terms = [term.lower() for term in (query or "").split() if term.strip()]
        candidates = self.list(include_archived=include_archived)
        if terms:
            candidates = tuple(
                entry
                for entry in candidates
                if any(term in entry.content.lower() for term in terms)
            )
        ranked = sorted(
            candidates,
            key=lambda item: (
                not item.pinned,
                -item.recall_count,
                -item.updated_at.timestamp(),
            ),
        )
        return deepcopy(tuple(ranked[: max(1, limit)]))

    def update(
        self,
        entry_id: str,
        *,
        observed_version: int,
        content: str | None = None,
        pinned: bool | None = None,
        archived: bool | None = None,
    ) -> MemoryEntry:
        entry = self._require_observed(entry_id, observed_version)
        if content is not None:
            self._validate_content((content or "").strip())
        updated = entry.with_update(
            content=content,
            pinned=pinned,
            archived=archived,
        )
        self._entries[entry_id] = deepcopy(updated)
        return deepcopy(updated)

    def touch_recall(self, entry_id: str, *, observed_version: int) -> MemoryEntry:
        entry = self._require_observed(entry_id, observed_version)
        updated = entry.with_update(recalled=True)
        self._entries[entry_id] = deepcopy(updated)
        return deepcopy(updated)

    def _require_observed(self, entry_id: str, observed_version: int) -> MemoryEntry:
        entry = self._entries.get(entry_id)
        if entry is None:
            raise MemoryStoreError(f"unknown memory id: {entry_id}")
        if observed_version != entry.version:
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
        active = [entry for entry in self._entries.values() if not entry.archived]
        if len(active) < self._max_entries:
            return
        candidates = [
            entry
            for entry in active
            if not entry.pinned and entry.kind is not MemoryKind.CONTROL_AXIOM
        ]
        if not candidates:
            raise MemoryStoreError("memory capacity reached; no compactable entries")
        candidates.sort(key=lambda item: (item.recall_count, item.updated_at.timestamp()))
        victim = candidates[0]
        self._entries[victim.id] = victim.with_update(archived=True)
