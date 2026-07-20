"""Tool protocol primitives and result/permission types.

Tools are plain Python classes (not ABCs — abstract classvars add more friction
than they prevent). Subclasses override the class-level ``name`` /
``description`` / ``permission`` / ``parameters_schema`` attributes and
implement the async ``execute()`` coroutine.

``ToolPermission`` encodes the runtime trust model:

* ``SAFE``    — read-only side effects; registry executes immediately.
* ``CONFIRM`` — mutating or externally-visible action; an interactive turn
                suspends until the user approves, while trusted host workflows
                may pass an explicit approval grant.
* ``DENY``    — never allowed. Registered tools with this tier are dropped at
                ``register()`` time so they cannot be invoked at all.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

from .metadata import (
    Evidence,
    RecommendedNextAction,
    SideEffect,
    ToolErrorType,
    ToolResultStatus,
)


class ToolPermission(str, Enum):
    SAFE = "safe"
    CONFIRM = "confirm"
    DENY = "deny"


@dataclass(slots=True)
class ToolResult:
    """Structured return value of a tool invocation.

    ``ok=True`` results are fed back to the LLM as-is under the ``tool`` role.
    ``ok=False`` results include status and error type in the tool-role message
    so the LLM can recover (retry with different args, pick a different tool,
    or tell the user it cannot proceed).
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
        self.evidence = tuple(self.evidence)
        self.side_effects = tuple(self.side_effects)
        if not self.ok and self.status is ToolResultStatus.SUCCESS:
            self.status = ToolResultStatus.ERROR

    @classmethod
    def failure(
        cls,
        error: str,
        *,
        error_type: ToolErrorType = ToolErrorType.UNKNOWN,
        recoverable_by_model: bool = False,
        recommended_next_action: RecommendedNextAction | None = None,
        source: str = "tool",
    ) -> "ToolResult":
        return cls(
            ok=False,
            content="",
            error=error,
            status=ToolResultStatus.ERROR,
            error_type=error_type,
            recoverable_by_model=recoverable_by_model,
            recommended_next_action=recommended_next_action,
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
            recommended_next_action=RecommendedNextAction.ASK_USER,
            source="permission",
        )

    def to_tool_message_content(self) -> str:
        if self.ok:
            return self.content
        prefix = f"[tool {self.status.value}]"
        if self.error_type is not None:
            prefix = f"{prefix} {self.error_type.value}:"
        return f"{prefix} {self.error or 'unknown error'}"

    def to_metadata(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status.value,
            "error_type": self.error_type.value if self.error_type else None,
            "recoverable_by_model": self.recoverable_by_model,
            "recommended_next_action": (
                self.recommended_next_action.value
                if self.recommended_next_action is not None
                else None
            ),
            "source": self.source,
            "evidence": [item.to_dict() for item in self.evidence],
            "side_effects": [item.to_dict() for item in self.side_effects],
        }


class Tool:
    """Base class for all tools.

    Subclasses set the four class-level attributes and override ``execute``.
    Instance state (e.g. a workspace root path) goes in ``__init__``.
    """

    name: str = ""
    description: str = ""
    permission: ToolPermission = ToolPermission.SAFE
    is_read_only: bool = True
    is_concurrency_safe: bool = False
    is_destructive: bool = False
    max_result_chars: int = 8_000
    search_hint: str = ""
    should_defer: bool = False
    always_load: bool = False
    interrupt_behavior: Literal["block", "cancel"] = "block"
    parameters_schema: dict[str, Any] = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    def activity_description(self, arguments: dict[str, Any]) -> str:
        return self.name

    def is_action_read_only(self, arguments: dict[str, Any] | None) -> bool:
        """Return True if THIS specific invocation has no side effects.

        v0.45 — multi-action tools (``cron_manage`` / ``mcp_manage``)
        bundle read-only ``list`` / ``inspect`` actions next to mutating
        ``create`` / ``edit`` / ``remove`` ones, and the tier-level
        ``permission=CONFIRM`` gate used to prompt the user for a
        yes/no even on the read-only paths. Subclasses **opt in** by
        overriding this hook to return True for the specific actions
        they consider read-only.

        Default follows the tool-level ``is_read_only`` contract. Confirm-tier
        multi-action tools override this hook when only some actions are safe.
        """
        return self.is_read_only

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:  # pragma: no cover - abstract
        raise NotImplementedError(f"{self.__class__.__name__}.execute not implemented")
