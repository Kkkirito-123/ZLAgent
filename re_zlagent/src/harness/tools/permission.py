"""Permission policy for bounded tool execution."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from .base import Tool, ToolPermission


class PermissionDecision(str, Enum):
    ALLOW = "allow"
    CONFIRM = "confirm"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class PermissionResult:
    decision: PermissionDecision
    allow_confirm: bool = False
    reason: str = ""
    read_only_action: bool = False
    trusted: bool = False


class PermissionPolicy:
    """Single permission decision point for tool invocations."""

    def decide_tool_call(
        self,
        *,
        tool: Tool | None,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        interactive: bool = True,
        trust_confirm_tools: bool = False,
    ) -> PermissionResult:
        args = arguments or {}
        if tool is None:
            return PermissionResult(
                PermissionDecision.DENY,
                reason=f"unknown tool: {tool_name}",
            )

        if tool.permission is ToolPermission.DENY:
            return PermissionResult(
                PermissionDecision.DENY,
                reason=f"tool '{tool_name}' is denied by policy",
            )

        if tool.permission is ToolPermission.SAFE:
            return PermissionResult(PermissionDecision.ALLOW)

        if self.is_action_read_only(tool, args):
            return PermissionResult(
                PermissionDecision.ALLOW,
                allow_confirm=True,
                read_only_action=True,
                reason="read-only action on confirm-tier tool",
            )

        if trust_confirm_tools:
            return PermissionResult(
                PermissionDecision.ALLOW,
                allow_confirm=True,
                trusted=True,
                reason="trusted confirm tool execution",
            )

        if not interactive:
            return PermissionResult(
                PermissionDecision.DENY,
                reason=(
                    f"tool '{tool_name}' requires confirmation, but this "
                    "run is non-interactive"
                ),
            )

        return PermissionResult(
            PermissionDecision.CONFIRM,
            reason=f"tool '{tool_name}' requires user confirmation",
        )

    @staticmethod
    def is_action_read_only(tool: Tool | None, arguments: dict[str, Any]) -> bool:
        if tool is None:
            return False
        try:
            return bool(tool.is_action_read_only(arguments))
        except Exception:
            return False

