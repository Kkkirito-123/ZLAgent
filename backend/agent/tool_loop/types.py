from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ...llm import LLMToolCall
    from ...tools import Tool, ToolResult
    from ..runtime import LoopOutcome


@dataclass(slots=True)
class PreparedToolCall:
    tc: "LLMToolCall"
    arguments: dict
    tool: Optional["Tool"]
    max_result_chars: int
    allow_confirm: bool = False


@dataclass(slots=True)
class ToolLoopControlState:
    budget: int
    expose_confirm: bool
    activated_deferred_tools: set[str]
    invoked_tool_names: list[str]
    tool_outcomes: list[tuple[str, bool, Optional[str]]]

    @classmethod
    def build(
        cls,
        *,
        default_budget: int,
        max_iterations: Optional[int],
        interactive: bool,
        trust_confirm_tools: bool,
    ) -> "ToolLoopControlState":
        return cls(
            budget=max_iterations if max_iterations is not None else default_budget,
            expose_confirm=interactive or trust_confirm_tools,
            activated_deferred_tools=set(),
            invoked_tool_names=[],
            tool_outcomes=[],
        )

    def outcome(
        self, final_text: str, *, failure_kind: Optional[str] = None,
    ) -> "LoopOutcome":
        from ..runtime import LoopOutcome
        return LoopOutcome(
            final_text=final_text,
            tool_call_count=len(self.invoked_tool_names),
            invoked_tool_names=tuple(self.invoked_tool_names),
            tool_outcomes=tuple(self.tool_outcomes),
            failure_kind=failure_kind,
        )
