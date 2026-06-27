"""Agent loop outcome model.

该模块保存一次 Agent 工具循环的返回模型。工具循环、确认恢复、轮次后处理都以
该类型交接结果，`AgentLoop` 只负责转发和协调。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ...gateways.base import DeliveryTarget, OutgoingMessage


@dataclass(slots=True)
class LoopOutcome:
    """表示一次 LLM 工具循环的结束结果。

    `final_text` 保存普通回复文本，`question_message` 保存待确认问题消息，
    `suspended_confirmation_id` 保存挂起的确认记录编号。工具调用数量、工具名称和
    工具结果用于轮次后处理、失败学习和复盘判断。
    """

    final_text: Optional[str] = None
    question_message: Optional[OutgoingMessage] = None
    suspended_confirmation_id: Optional[int] = None
    tool_call_count: int = 0
    invoked_tool_names: tuple[str, ...] = ()
    tool_outcomes: tuple[tuple[str, bool, Optional[str]], ...] = ()
    failure_kind: Optional[str] = None

    def to_message(self, *, reply_target: DeliveryTarget) -> Optional[OutgoingMessage]:
        """把循环结果转换为可发送消息。

        `reply_target` 是消息发送目标。存在确认问题时返回确认消息，存在普通文本时
        返回普通消息；二者均为空时返回空值。
        """

        if self.question_message is not None:
            return self.question_message
        if self.final_text is not None:
            return OutgoingMessage(target=reply_target, text=self.final_text)
        return None
