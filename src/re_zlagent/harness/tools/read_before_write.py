"""Read-before-write policy primitives.

This module does not perform filesystem IO. It models the evidence gate that
mutation tools can use later: a read stamps a target/hash mark, and a write
must present a matching precondition before editing an existing target.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class WritePreconditionStatus(str, Enum):
    ALLOW = "allow"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class ReadMark:
    target: str
    content_hash: str
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WritePrecondition:
    status: WritePreconditionStatus
    reason: str = ""
    required_action: str = ""

    @property
    def allowed(self) -> bool:
        return self.status is WritePreconditionStatus.ALLOW


class ReadBeforeWritePolicy:
    """In-memory read mark gate for mutation preconditions."""

    def __init__(self) -> None:
        self._marks: dict[str, ReadMark] = {}

    def stamp_read(
        self,
        *,
        target: str,
        content_hash: str,
        metadata: dict[str, Any] | None = None,
    ) -> ReadMark:
        mark = ReadMark(
            target=target,
            content_hash=content_hash,
            metadata=dict(metadata or {}),
        )
        self._marks[target] = mark
        return mark

    def latest_mark(self, target: str) -> ReadMark | None:
        return self._marks.get(target)

    def check_write(
        self,
        *,
        target: str,
        current_hash: str | None,
        creating_new_target: bool = False,
    ) -> WritePrecondition:
        if creating_new_target:
            return WritePrecondition(WritePreconditionStatus.ALLOW)
        if current_hash is None:
            return WritePrecondition(
                WritePreconditionStatus.DENY,
                reason="current target hash is required before mutating existing target",
                required_action="read_before_write",
            )
        mark = self.latest_mark(target)
        if mark is None:
            return WritePrecondition(
                WritePreconditionStatus.DENY,
                reason="target has not been read in this context",
                required_action="read_before_write",
            )
        if mark.content_hash != current_hash:
            return WritePrecondition(
                WritePreconditionStatus.DENY,
                reason="target changed since last read",
                required_action="read_before_write",
            )
        return WritePrecondition(WritePreconditionStatus.ALLOW)

