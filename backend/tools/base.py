"""Tool protocol primitives and result/permission types.

Tools are plain Python classes (not ABCs — abstract classvars add more friction
than they prevent). Subclasses override the class-level ``name`` /
``description`` / ``permission`` / ``parameters_schema`` attributes and
implement the async ``execute()`` coroutine.

``ToolPermission`` encodes the v0.5 trust model:

* ``SAFE``    — read-only side effects; registry executes immediately.
* ``CONFIRM`` — mutating or externally-visible action; registry refuses in v0.5
                (the confirmation broker lands in v0.6). These tools are also
                omitted from the LLM-facing schema by default so the model does
                not waste tokens trying to call something that will be denied.
* ``DENY``    — never allowed. Registered tools with this tier are dropped at
                ``register()`` time so they cannot be invoked at all.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal


class ToolPermission(str, Enum):
    SAFE = "safe"
    CONFIRM = "confirm"
    DENY = "deny"


@dataclass(slots=True)
class ToolResult:
    """Structured return value of a tool invocation.

    ``ok=True`` results are fed back to the LLM as-is under the ``tool`` role.
    ``ok=False`` results are surfaced as ``[tool error] ...`` strings so the
    LLM can recover (retry with different args, pick a different tool, or tell
    the user it cannot proceed).
    """

    ok: bool
    content: str
    error: str | None = None
    raw: dict[str, Any] | None = None

    def to_tool_message_content(self) -> str:
        if self.ok:
            return self.content
        return f"[tool error] {self.error or 'unknown error'}"


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

        Default is ``False`` — historical behaviour where the confirm
        flow gates every invocation. The ``is_read_only`` class flag
        is *not* consulted here because SAFE-tier tools already bypass
        the confirm path entirely, so the only callers of this hook
        are CONFIRM-tier multi-action tools that explicitly opt-in.
        """
        return False

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:  # pragma: no cover - abstract
        raise NotImplementedError(f"{self.__class__.__name__}.execute not implemented")
