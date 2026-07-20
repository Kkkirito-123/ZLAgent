"""工具权限策略。

该模块把工具权限判断从工具循环中拆出，作为 Harness 的独立策略层。
safe 只允许只读工具直接执行；confirm 在交互轮次挂起确认，非交互轮次
拒绝，参数级只读动作和受信任后台复盘可通过显式主机授权执行。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from .base import Tool, ToolPermission


class PermissionDecision(str, Enum):
    """工具调用权限判断结果。"""

    ALLOW = "allow"
    CONFIRM = "confirm"
    DENY = "deny"


@dataclass(slots=True)
class PermissionResult:
    """单次工具调用的权限判断结果。

    ``allow_confirm`` 表示执行时可以把 confirm 等级工具视作已确认。
    ``reason`` 用于写入日志或回填给 LLM，避免执行层再拼装权限原因。
    """

    decision: PermissionDecision
    allow_confirm: bool = False
    reason: str = ""
    read_only_action: bool = False


@dataclass(slots=True)
class PermissionPolicy:
    """统一工具权限策略，不按平台静默放行写操作。"""

    def decide_tool_call(
        self,
        *,
        tool: Optional[Tool],
        tool_name: str,
        arguments: dict[str, Any],
        platform: str,
        interactive: bool,
        trust_confirm_tools: bool,
    ) -> PermissionResult:
        """判断一次工具调用是否允许执行、需要确认或应当拒绝。"""
        if tool is None:
            return PermissionResult(PermissionDecision.ALLOW)

        if tool.permission is ToolPermission.DENY:
            return PermissionResult(
                PermissionDecision.DENY,
                reason=f"tool '{tool_name}' is denied by policy.",
            )

        if tool.permission is ToolPermission.SAFE:
            return PermissionResult(PermissionDecision.ALLOW)

        read_only_action = self.is_action_read_only(tool, arguments)
        if read_only_action:
            return PermissionResult(
                PermissionDecision.ALLOW,
                allow_confirm=True,
                read_only_action=True,
            )

        if trust_confirm_tools:
            return PermissionResult(
                PermissionDecision.ALLOW,
                allow_confirm=True,
                reason="trusted confirm tool execution",
            )

        if not interactive:
            return PermissionResult(
                PermissionDecision.DENY,
                reason=(
                    f"tool '{tool_name}' requires user confirmation, but this "
                    "turn is non-interactive (cron). Try a safe-tier tool or skip this step."
                ),
            )

        return PermissionResult(
            PermissionDecision.CONFIRM,
            reason=f"tool '{tool_name}' requires user confirmation.",
        )

    def is_action_read_only(self, tool: Optional[Tool], arguments: dict[str, Any]) -> bool:
        """判断 confirm 多 action 工具的本次调用是否为只读动作。"""
        if tool is None:
            return False
        try:
            return bool(tool.is_action_read_only(arguments))
        except Exception:  # noqa: BLE001
            return False
