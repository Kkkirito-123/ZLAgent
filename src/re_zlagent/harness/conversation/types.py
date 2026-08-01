"""Conversation-session types kept separate from durable task truth."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from html import escape
from typing import Any

from re_zlagent.harness.model import estimate_text_tokens


def utc_now() -> datetime:
    """Return one timezone-aware UTC timestamp."""

    return datetime.now(timezone.utc)


class CompactionReason(str, Enum):
    """Why older conversation turns were compacted."""

    ROUND_THRESHOLD = "round_threshold"
    TOKEN_THRESHOLD = "token_threshold"


@dataclass(frozen=True, slots=True)
class ConversationTurn:
    """One atomic user/assistant exchange in a named session."""

    session_id: str
    sequence: int
    user_content: str
    assistant_content: str
    archived: bool = False
    created_at: datetime = field(default_factory=utc_now)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_session_id(self.session_id)
        if self.sequence < 1:
            raise ValueError("conversation turn sequence must be >= 1")
        if not self.user_content.strip():
            raise ValueError("conversation user content must be non-empty")
        if not self.assistant_content.strip():
            raise ValueError("conversation assistant content must be non-empty")
        object.__setattr__(self, "user_content", self.user_content.strip())
        object.__setattr__(
            self,
            "assistant_content",
            self.assistant_content.strip(),
        )
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def estimated_tokens(self) -> int:
        """Conservatively estimate this complete exchange."""

        return (
            estimate_text_tokens(self.user_content)
            + estimate_text_tokens(self.assistant_content)
            + 12
        )


@dataclass(frozen=True, slots=True)
class ConversationCompaction:
    """Append-only rolling summary of a contiguous turn prefix."""

    id: str
    session_id: str
    summarized_through_sequence: int
    summary: str
    reason: CompactionReason
    source_turn_count: int
    estimated_input_tokens: int
    estimated_output_tokens: int
    created_at: datetime = field(default_factory=utc_now)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("conversation compaction id must be non-empty")
        _validate_session_id(self.session_id)
        if self.summarized_through_sequence < 1:
            raise ValueError(
                "summarized_through_sequence must be >= 1"
            )
        if not self.summary.strip():
            raise ValueError("conversation summary must be non-empty")
        if self.source_turn_count < 1:
            raise ValueError("source_turn_count must be >= 1")
        if self.estimated_input_tokens < 0:
            raise ValueError("estimated_input_tokens must be >= 0")
        if self.estimated_output_tokens < 0:
            raise ValueError("estimated_output_tokens must be >= 0")
        object.__setattr__(self, "summary", self.summary.strip())
        object.__setattr__(self, "metadata", dict(self.metadata))


@dataclass(frozen=True, slots=True)
class ConversationContext:
    """Bounded, read-only context selected for one model request."""

    session_id: str
    summary: str = ""
    recent_turns: tuple[ConversationTurn, ...] = field(default_factory=tuple)
    summarized_through_sequence: int = 0
    stored_turn_count: int = 0
    omitted_recent_turn_count: int = 0

    def __post_init__(self) -> None:
        _validate_session_id(self.session_id)
        object.__setattr__(self, "summary", self.summary.strip())
        object.__setattr__(self, "recent_turns", tuple(self.recent_turns))
        if self.summarized_through_sequence < 0:
            raise ValueError("summarized_through_sequence must be >= 0")
        if self.stored_turn_count < 0 or self.omitted_recent_turn_count < 0:
            raise ValueError("conversation turn counts must be >= 0")

    @property
    def recent_tokens(self) -> int:
        return sum(turn.estimated_tokens for turn in self.recent_turns)

    def render_recent(self) -> str:
        """Render complete turns while preserving their atomic boundaries."""

        blocks: list[str] = []
        for turn in self.recent_turns:
            blocks.append(
                f"<turn sequence=\"{turn.sequence}\">\n"
                f"<user>\n{escape(turn.user_content)}\n</user>\n"
                f"<assistant>\n{escape(turn.assistant_content)}\n</assistant>\n"
                "</turn>"
            )
        return "\n\n".join(blocks)

    def render_summary(self) -> str:
        """Escape model-produced summary text before placing it in a fence."""

        return escape(self.summary)

    def metadata_dict(self) -> dict[str, Any]:
        """Return content-free observability metadata."""

        return {
            "session_id": self.session_id,
            "summary_present": bool(self.summary),
            "summary_estimated_tokens": estimate_text_tokens(self.summary),
            "summarized_through_sequence": self.summarized_through_sequence,
            "stored_turn_count": self.stored_turn_count,
            "recent_turn_count": len(self.recent_turns),
            "recent_estimated_tokens": self.recent_tokens,
            "recent_sequences": [turn.sequence for turn in self.recent_turns],
            "omitted_recent_turn_count": self.omitted_recent_turn_count,
        }


@dataclass(frozen=True, slots=True)
class ConversationUpdate:
    """Observable result of recording and optionally compacting one turn."""

    turn: ConversationTurn
    compaction: ConversationCompaction | None = None
    compaction_triggered: bool = False
    compaction_error_type: str | None = None

    def metadata_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "recorded": True,
            "sequence": self.turn.sequence,
            "compaction_triggered": self.compaction_triggered,
            "compacted": self.compaction is not None,
        }
        if self.compaction is not None:
            data.update(
                {
                    "compaction_reason": self.compaction.reason.value,
                    "summarized_through_sequence": (
                        self.compaction.summarized_through_sequence
                    ),
                    "compaction_source_turn_count": (
                        self.compaction.source_turn_count
                    ),
                    "compaction_estimated_input_tokens": (
                        self.compaction.estimated_input_tokens
                    ),
                    "compaction_estimated_output_tokens": (
                        self.compaction.estimated_output_tokens
                    ),
                }
            )
            usage = self.compaction.metadata.get("usage")
            if isinstance(usage, dict):
                data["usage"] = dict(usage)
        if self.compaction_error_type is not None:
            data["compaction_error_type"] = self.compaction_error_type
        return data


def _validate_session_id(session_id: str) -> None:
    if not session_id.strip():
        raise ValueError("conversation session_id must be non-empty")
    if session_id != session_id.strip():
        raise ValueError(
            "conversation session_id must not have surrounding whitespace"
        )
    if len(session_id) > 200:
        raise ValueError("conversation session_id must be at most 200 chars")
    if any(ord(char) < 32 for char in session_id):
        raise ValueError("conversation session_id must not contain control chars")
