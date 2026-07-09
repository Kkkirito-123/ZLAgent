"""Model client boundaries."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class ModelMessage:
    """One chat-style model message."""

    role: str
    content: str

    def __post_init__(self) -> None:
        if self.role not in {"system", "user", "assistant"}:
            raise ValueError(f"unsupported model message role: {self.role}")
        if not isinstance(self.content, str):
            raise ValueError("message.content must be a string")


@dataclass(frozen=True, slots=True)
class ModelResponse:
    """Model response payload."""

    content: str
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.content, str):
            raise ValueError("response.content must be a string")
        object.__setattr__(self, "raw", dict(self.raw))


class ModelClient(Protocol):
    """LLM provider boundary."""

    async def complete(self, messages: tuple[ModelMessage, ...]) -> ModelResponse:
        """Return a model response for messages."""
