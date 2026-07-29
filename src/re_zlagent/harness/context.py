"""Small observable context-budget primitives shared by Agent and Runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from math import ceil
from typing import Any, Iterable


class ContextTrust(str, Enum):
    """How a context segment may be interpreted by a model."""

    HOST = "host"
    UNTRUSTED = "untrusted"
    RECALLED_BACKGROUND = "recalled_background"
    RUNTIME_STATE = "runtime_state"


@dataclass(frozen=True, slots=True)
class ContextInput:
    """One candidate segment before the global context budget is applied."""

    id: str
    source: str
    trust: ContextTrust
    content: str
    max_chars: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("context input id must be non-empty")
        if not self.source.strip():
            raise ValueError("context input source must be non-empty")
        if self.max_chars is not None and self.max_chars < 0:
            raise ValueError("context input max_chars must be >= 0")
        object.__setattr__(self, "metadata", dict(self.metadata))


@dataclass(frozen=True, slots=True)
class ContextSegment:
    """One included, bounded context segment."""

    id: str
    source: str
    trust: ContextTrust
    content: str
    original_chars: int
    included_chars: int
    truncated: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("context segment id must be non-empty")
        if not self.source.strip():
            raise ValueError("context segment source must be non-empty")
        if self.original_chars < 0 or self.included_chars < 0:
            raise ValueError("context segment character counts must be >= 0")
        if self.included_chars != len(self.content):
            raise ValueError("included_chars must match context content length")
        if self.original_chars < self.included_chars:
            raise ValueError("original_chars must be >= included_chars")
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self, *, include_content: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "source": self.source,
            "trust": self.trust.value,
            "original_chars": self.original_chars,
            "included_chars": self.included_chars,
            "estimated_tokens": ceil(self.included_chars / 4),
            "truncated": self.truncated,
            "metadata": dict(self.metadata),
        }
        if include_content:
            data["content"] = self.content
        return data


@dataclass(frozen=True, slots=True)
class ContextManifest:
    """Observable manifest for the exact bounded context supplied to a model."""

    budget_chars: int
    segments: tuple[ContextSegment, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.budget_chars < 0:
            raise ValueError("context budget_chars must be >= 0")
        object.__setattr__(self, "segments", tuple(self.segments))
        ids = [segment.id for segment in self.segments]
        if len(ids) != len(set(ids)):
            raise ValueError("context segment ids must be unique")
        if self.used_chars > self.budget_chars:
            raise ValueError("context manifest exceeds its character budget")

    @property
    def used_chars(self) -> int:
        return sum(segment.included_chars for segment in self.segments)

    @property
    def estimated_tokens(self) -> int:
        return sum(
            ceil(segment.included_chars / 4)
            for segment in self.segments
        )

    @property
    def truncated(self) -> bool:
        return any(segment.truncated for segment in self.segments)

    def render(self) -> str:
        """Render fenced context while keeping trust labels visible."""

        blocks: list[str] = []
        for segment in self.segments:
            blocks.append(
                (
                    f'<context-segment id="{segment.id}" '
                    f'source="{segment.source}" trust="{segment.trust.value}">\n'
                    f"{segment.content}\n"
                    "</context-segment>"
                )
            )
        return "\n\n".join(blocks)

    def to_dict(self, *, include_content: bool = False) -> dict[str, Any]:
        return {
            "budget_chars": self.budget_chars,
            "used_chars": self.used_chars,
            "estimated_tokens": self.estimated_tokens,
            "truncated": self.truncated,
            "segments": [
                segment.to_dict(include_content=include_content)
                for segment in self.segments
            ],
        }


class ContextManifestBuilder:
    """Apply one deterministic character budget in caller-supplied priority order."""

    def __init__(self, *, max_chars: int = 6_000) -> None:
        if max_chars < 256:
            raise ValueError("context max_chars must be at least 256")
        self._max_chars = max_chars

    def build(self, inputs: Iterable[ContextInput]) -> ContextManifest:
        remaining = self._max_chars
        segments: list[ContextSegment] = []
        seen: set[str] = set()
        for item in inputs:
            if item.id in seen:
                raise ValueError(f"duplicate context input id: {item.id}")
            seen.add(item.id)
            if not item.content or remaining <= 0:
                continue
            limit = remaining
            if item.max_chars is not None:
                limit = min(limit, item.max_chars)
            content, truncated = _truncate(item.content, limit)
            if not content:
                continue
            segments.append(
                ContextSegment(
                    id=item.id,
                    source=item.source,
                    trust=item.trust,
                    content=content,
                    original_chars=len(item.content),
                    included_chars=len(content),
                    truncated=truncated,
                    metadata=item.metadata,
                )
            )
            remaining -= len(content)
        return ContextManifest(
            budget_chars=self._max_chars,
            segments=tuple(segments),
        )


def _truncate(value: str, limit: int) -> tuple[str, bool]:
    if len(value) <= limit:
        return value, False
    if limit <= 0:
        return "", True
    marker = "\n[...truncated by host...]"
    if limit <= len(marker):
        return marker[:limit], True
    return value[: limit - len(marker)] + marker, True
