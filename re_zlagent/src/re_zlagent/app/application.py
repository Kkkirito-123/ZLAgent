"""Application service that connects gateways to the harness agent."""

from __future__ import annotations

from dataclasses import dataclass

from re_zlagent.gateway import IncomingMessage, OutgoingMessage
from re_zlagent.harness.agent import AgentOrchestrator, AgentRunRequest, AgentRunResult
from re_zlagent.harness.tasking import TaskRunStatus


@dataclass(frozen=True, slots=True)
class ApplicationResult:
    """Application-level result for one inbound message."""

    agent_result: AgentRunResult
    outgoing: OutgoingMessage


class AgentApplication:
    """Thin app layer.

    This layer owns message normalization and response formatting. It does not
    execute tools, mutate memory, or decide task acceptance.
    """

    def __init__(self, *, orchestrator: AgentOrchestrator) -> None:
        self._orchestrator = orchestrator

    async def handle_message(self, message: IncomingMessage) -> ApplicationResult:
        request = AgentRunRequest(
            run_id=f"msg-{message.id}",
            user_goal=message.text,
            context={
                "platform": message.platform,
                "user_id": message.user_id,
                "message_metadata": message.metadata,
            },
            interactive=True,
        )
        agent_result = await self._orchestrator.run(request)
        outgoing = OutgoingMessage(
            target=message.reply_to,
            text=self._format_reply(agent_result),
            metadata={
                "run_id": agent_result.runtime_result.run.id,
                "status": agent_result.runtime_result.run.status.value,
                "accepted": agent_result.accepted,
            },
        )
        return ApplicationResult(agent_result=agent_result, outgoing=outgoing)

    def _format_reply(self, result: AgentRunResult) -> str:
        status = result.runtime_result.run.status
        if result.accepted and status is TaskRunStatus.COMPLETED:
            return f"已完成：{result.request.user_goal}"
        if status is TaskRunStatus.WAITING_USER:
            return "需要用户确认或补充信息。"
        if status is TaskRunStatus.ACCEPTANCE_FAILED:
            return "任务未通过验收。"
        if result.runtime_result.failure is not None:
            return f"任务失败：{result.runtime_result.failure.root_cause}"
        return f"任务状态：{status.value}"
