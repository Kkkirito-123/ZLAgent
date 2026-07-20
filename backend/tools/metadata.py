"""Machine-readable metadata carried by tool results."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ToolResultStatus(str, Enum):
    SUCCESS = "success"
    ERROR = "error"
    DENIED = "denied"
    REQUIRES_CONFIRMATION = "requires_confirmation"
    PARTIAL = "partial"


class ToolErrorType(str, Enum):
    INVALID_INPUT = "invalid_input"
    PERMISSION_DENIED = "permission_denied"
    NOT_FOUND = "not_found"
    EXTERNAL_UNAVAILABLE = "external_unavailable"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    CONFLICT = "conflict"
    UNSAFE_WRITE = "unsafe_write"
    TOOL_EXCEPTION = "tool_exception"
    UNKNOWN = "unknown"


class RecommendedNextAction(str, Enum):
    RETRY = "retry"
    ASK_USER = "ask_user"
    READ_BEFORE_WRITE = "read_before_write"
    USE_ALTERNATIVE_TOOL = "use_alternative_tool"
    STOP = "stop"
    MANUAL_REVIEW = "manual_review"


@dataclass(frozen=True, slots=True)
class Evidence:
    type: str
    ref: str
    summary: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "ref": self.ref,
            "summary": self.summary,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class SideEffect:
    type: str
    target: str
    risk: str = "low"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "target": self.target,
            "risk": self.risk,
            "metadata": dict(self.metadata),
        }


__all__ = [
    "Evidence",
    "RecommendedNextAction",
    "SideEffect",
    "ToolErrorType",
    "ToolResultStatus",
]
