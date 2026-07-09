"""Memory domain types for the harness."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class MemoryKind(str, Enum):
    """Durable memory categories."""

    CONTROL_AXIOM = "control_axiom"
    USER_FACT = "user_fact"
    AGENT_NOTE = "agent_note"


class MemorySource(str, Enum):
    """Where a memory entry came from."""

    EXPLICIT = "explicit"
    REVIEW = "review"
    IMPORT = "import"


@dataclass(frozen=True, slots=True)
class MemoryEntry:
    """Versioned durable memory entry."""

    id: str
    kind: MemoryKind
    content: str
    source: MemorySource = MemorySource.EXPLICIT
    pinned: bool = False
    archived: bool = False
    version: int = 1
    recall_count: int = 0
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    last_recalled_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("memory.id must be non-empty")
        if not self.content.strip():
            raise ValueError("memory.content must be non-empty")
        if self.version < 1:
            raise ValueError("memory.version must be >= 1")
        if self.recall_count < 0:
            raise ValueError("memory.recall_count must be >= 0")
        object.__setattr__(self, "content", self.content.strip())
        object.__setattr__(self, "metadata", dict(self.metadata))

    def with_update(
        self,
        *,
        content: str | None = None,
        pinned: bool | None = None,
        archived: bool | None = None,
        recalled: bool = False,
    ) -> "MemoryEntry":
        return MemoryEntry(
            id=self.id,
            kind=self.kind,
            content=self.content if content is None else content,
            source=self.source,
            pinned=self.pinned if pinned is None else pinned,
            archived=self.archived if archived is None else archived,
            version=self.version + 1,
            recall_count=self.recall_count + (1 if recalled else 0),
            created_at=self.created_at,
            updated_at=utc_now(),
            last_recalled_at=utc_now() if recalled else self.last_recalled_at,
            metadata=self.metadata,
        )


@dataclass(frozen=True, slots=True)
class MemoryWriteResult:
    """Result of a memory mutation."""

    entry: MemoryEntry
    created: bool = False
