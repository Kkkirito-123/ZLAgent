"""Core tool protocol primitives for the re_zlagent harness."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, ClassVar, Literal

from .metadata import (
    Evidence,
    RecommendedNextAction,
    SideEffect,
    ToolErrorType,
    ToolResultStatus,
)


class ToolPermission(str, Enum):
    """Trust tier for a tool."""

    SAFE = "safe"
    CONFIRM = "confirm"
    DENY = "deny"


@dataclass(slots=True)
class ToolResult:
    """Structured return value from a tool invocation.

    ``content`` remains the model-facing readable payload. Metadata fields are
    for checkpointing, acceptance, trace, and deterministic tests.
    """

    ok: bool
    content: str
    error: str | None = None
    raw: dict[str, Any] | None = None
    status: ToolResultStatus = ToolResultStatus.SUCCESS
    error_type: ToolErrorType | None = None
    recoverable_by_model: bool = False
    recommended_next_action: RecommendedNextAction | None = None
    source: str = "tool"
    evidence: tuple[Evidence, ...] = field(default_factory=tuple)
    side_effects: tuple[SideEffect, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.evidence, tuple):
            self.evidence = tuple(self.evidence)
        if not isinstance(self.side_effects, tuple):
            self.side_effects = tuple(self.side_effects)
        if self.ok and self.status is ToolResultStatus.ERROR:
            raise ValueError("ok=True cannot use status=error")
        if not self.ok and self.status is ToolResultStatus.SUCCESS:
            self.status = ToolResultStatus.ERROR

    @classmethod
    def success(
        cls,
        content: str,
        *,
        raw: dict[str, Any] | None = None,
        evidence: tuple[Evidence, ...] | list[Evidence] = (),
        side_effects: tuple[SideEffect, ...] | list[SideEffect] = (),
        source: str = "tool",
    ) -> "ToolResult":
        return cls(
            ok=True,
            content=content,
            raw=raw,
            status=ToolResultStatus.SUCCESS,
            evidence=tuple(evidence),
            side_effects=tuple(side_effects),
            source=source,
        )

    @classmethod
    def partial(
        cls,
        content: str,
        *,
        error: str | None = None,
        raw: dict[str, Any] | None = None,
        evidence: tuple[Evidence, ...] | list[Evidence] = (),
        side_effects: tuple[SideEffect, ...] | list[SideEffect] = (),
        source: str = "tool",
    ) -> "ToolResult":
        return cls(
            ok=True,
            content=content,
            error=error,
            raw=raw,
            status=ToolResultStatus.PARTIAL,
            evidence=tuple(evidence),
            side_effects=tuple(side_effects),
            source=source,
        )

    @classmethod
    def failure(
        cls,
        error: str,
        *,
        error_type: ToolErrorType = ToolErrorType.UNKNOWN,
        recoverable_by_model: bool = False,
        recommended_next_action: RecommendedNextAction | None = None,
        raw: dict[str, Any] | None = None,
        evidence: tuple[Evidence, ...] | list[Evidence] = (),
        source: str = "tool",
    ) -> "ToolResult":
        return cls(
            ok=False,
            content="",
            error=error,
            raw=raw,
            status=ToolResultStatus.ERROR,
            error_type=error_type,
            recoverable_by_model=recoverable_by_model,
            recommended_next_action=recommended_next_action,
            evidence=tuple(evidence),
            source=source,
        )

    @classmethod
    def denied(cls, reason: str) -> "ToolResult":
        return cls(
            ok=False,
            content="",
            error=reason,
            status=ToolResultStatus.DENIED,
            error_type=ToolErrorType.PERMISSION_DENIED,
            recoverable_by_model=False,
            recommended_next_action=RecommendedNextAction.STOP,
            source="permission",
        )

    @classmethod
    def requires_confirmation(cls, reason: str) -> "ToolResult":
        return cls(
            ok=False,
            content="",
            error=reason,
            status=ToolResultStatus.REQUIRES_CONFIRMATION,
            error_type=ToolErrorType.PERMISSION_DENIED,
            recoverable_by_model=False,
            recommended_next_action=RecommendedNextAction.ASK_USER,
            source="permission",
        )

    def to_tool_message_content(self) -> str:
        """Return readable content suitable for an LLM tool message."""

        if self.ok:
            return self.content
        prefix = f"[tool {self.status.value}]"
        if self.error_type is not None:
            prefix = f"{prefix} {self.error_type.value}:"
        return f"{prefix} {self.error or 'unknown error'}"

    def to_metadata(self) -> dict[str, Any]:
        """Return deterministic metadata for trace/checkpoint/eval code."""

        return {
            "ok": self.ok,
            "status": self.status.value,
            "error_type": self.error_type.value if self.error_type else None,
            "recoverable_by_model": self.recoverable_by_model,
            "recommended_next_action": (
                self.recommended_next_action.value
                if self.recommended_next_action
                else None
            ),
            "source": self.source,
            "evidence": [item.to_dict() for item in self.evidence],
            "side_effects": [item.to_dict() for item in self.side_effects],
        }


class Tool:
    """Base class for bounded harness tools."""

    name: ClassVar[str] = ""
    description: ClassVar[str] = ""
    permission: ClassVar[ToolPermission] = ToolPermission.SAFE
    is_read_only: ClassVar[bool] = True
    is_concurrency_safe: ClassVar[bool] = False
    is_destructive: ClassVar[bool] = False
    side_effects: ClassVar[tuple[str, ...]] = ()
    max_result_chars: ClassVar[int] = 8_000
    interrupt_behavior: ClassVar[Literal["block", "cancel"]] = "block"
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    def activity_description(self, arguments: dict[str, Any]) -> str:
        return self.name

    def is_action_read_only(self, arguments: dict[str, Any] | None) -> bool:
        """Return True when this specific invocation is read-only."""

        return self.is_read_only

    def requires_read_before_write(self, arguments: dict[str, Any] | None) -> bool:
        """Return True when mutation requires a fresh read mark."""

        return False

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        raise NotImplementedError(f"{self.__class__.__name__}.execute not implemented")
