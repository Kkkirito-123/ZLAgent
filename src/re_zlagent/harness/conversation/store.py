"""Conversation persistence protocol and in-memory behavior baseline."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from threading import RLock
from typing import Any, Protocol

from .types import (
    ConversationCompaction,
    ConversationTurn,
    _validate_session_id,
)


class ConversationStoreError(RuntimeError):
    """Conversation persistence contract failure."""


class ConversationStore(Protocol):
    """Persistence boundary for raw turns and append-only compactions."""

    def append_turn(
        self,
        session_id: str,
        user_content: str,
        assistant_content: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> ConversationTurn:
        """Append one atomic exchange and return its session sequence."""

    def list_turns(
        self,
        session_id: str,
        *,
        after_sequence: int = 0,
        include_archived: bool = False,
    ) -> tuple[ConversationTurn, ...]:
        """List turns in ascending sequence order."""

    def latest_compaction(
        self,
        session_id: str,
    ) -> ConversationCompaction | None:
        """Return the newest summary prefix for a session."""

    def append_compaction(
        self,
        compaction: ConversationCompaction,
    ) -> ConversationCompaction:
        """Append a strictly advancing summary prefix."""


class InMemoryConversationStore:
    """Thread-safe in-memory reference adapter."""

    def __init__(self, *, raw_retention_rounds: int = 32) -> None:
        if raw_retention_rounds < 1:
            raise ValueError("raw_retention_rounds must be >= 1")
        self._raw_retention_rounds = raw_retention_rounds
        self._turns: dict[str, list[ConversationTurn]] = {}
        self._compactions: dict[str, list[ConversationCompaction]] = {}
        self._lock = RLock()

    def append_turn(
        self,
        session_id: str,
        user_content: str,
        assistant_content: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> ConversationTurn:
        _validate_session_id(session_id)
        with self._lock:
            turns = self._turns.setdefault(session_id, [])
            turn = ConversationTurn(
                session_id=session_id,
                sequence=len(turns) + 1,
                user_content=user_content,
                assistant_content=assistant_content,
                metadata=metadata or {},
            )
            turns.append(turn)
            hot_boundary = max(1, turn.sequence - self._raw_retention_rounds + 1)
            self._turns[session_id] = [
                replace(item, archived=item.sequence < hot_boundary)
                for item in turns
            ]
            return deepcopy(turn)

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
        with self._lock:
            turns = (
                item
                for item in self._turns.get(session_id, ())
                if item.sequence > after_sequence
                and (include_archived or not item.archived)
            )
            return deepcopy(tuple(turns))

    def latest_compaction(
        self,
        session_id: str,
    ) -> ConversationCompaction | None:
        _validate_session_id(session_id)
        with self._lock:
            items = self._compactions.get(session_id, ())
            return deepcopy(items[-1]) if items else None

    def append_compaction(
        self,
        compaction: ConversationCompaction,
    ) -> ConversationCompaction:
        with self._lock:
            items = self._compactions.setdefault(compaction.session_id, [])
            if any(item.id == compaction.id for item in items):
                existing = next(item for item in items if item.id == compaction.id)
                if existing == compaction:
                    return deepcopy(existing)
                raise ConversationStoreError(
                    f"conflicting conversation compaction id: {compaction.id}"
                )
            if (
                items
                and compaction.summarized_through_sequence
                <= items[-1].summarized_through_sequence
            ):
                raise ConversationStoreError(
                    "conversation compaction must advance the summarized prefix"
                )
            items.append(deepcopy(compaction))
            return deepcopy(compaction)
