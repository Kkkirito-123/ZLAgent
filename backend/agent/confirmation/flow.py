from __future__ import annotations

from typing import Optional, TYPE_CHECKING

from loguru import logger

from ...db.confirmations import (
    STATUS_APPROVED, STATUS_DENIED, STATUS_RESOLVED,
    ConfirmationSnapshot,
)
from ...gateways.base import DeliveryTarget, IncomingMessage, OutgoingMessage
from ...llm import LLMMessage
from ...tools import ToolResult
from . import formatting as _lc
from ..tool_loop.runner import _append_tool_message, _parse_tool_call_args

if TYPE_CHECKING:
    from ...db.confirmations import ConfirmationStore
    from ...llm import LLMToolCall
    from ...tools import ToolRegistry
    from ..runtime import LoopOutcome


class ConfirmationFlow:
    def __init__(
        self,
        *,
        store: Optional["ConfirmationStore"],
        registry: Optional["ToolRegistry"],
        memory_sync_fn,
        run_tool_loop_fn,
        system_prompt_dm: str,
    ) -> None:
        self._store = store
        self._registry = registry
        self._memory_sync_fn = memory_sync_fn
        self._run_tool_loop_fn = run_tool_loop_fn
        self._system_prompt_dm = system_prompt_dm

    async def resume(
        self,
        pending: ConfirmationSnapshot,
        decision: bool,
        message: IncomingMessage,
        *,
        llm_configured: bool,
    ) -> Optional[OutgoingMessage]:
        assert self._store is not None
        assert message.reply_target is not None

        if not llm_configured or self._registry is None:
            return self._resume_unavailable(pending=pending, message=message)

        tool_result = await self._resolve_tool_result(pending, decision=decision, user_id=message.user_id)
        status = "approved" if decision else "denied"
        direct_reply = _lc.direct_confirmation_reply(pending.tool_name, pending.tool_arguments, decision, tool_result)
        if direct_reply is not None:
            return self._resume_direct_reply(
                pending=pending,
                message=message,
                decision=decision,
                tool_result=tool_result,
                direct_reply=direct_reply,
                status=status,
            )
        return await self._resume_with_tool_loop(
            pending=pending,
            message=message,
            tool_result=tool_result,
            status=status,
        )

    def _resume_unavailable(
        self,
        *,
        pending: ConfirmationSnapshot,
        message: IncomingMessage,
    ) -> OutgoingMessage:
        assert self._store is not None
        self._store.mark(pending.id, status=STATUS_DENIED, outcome="loop unavailable on resume")
        return OutgoingMessage(target=message.reply_target, text="[ZLAgent] 确认无法恢复：LLM 或工具注册表已不可用")

    async def _resolve_tool_result(
        self,
        pending: ConfirmationSnapshot,
        *,
        decision: bool,
        user_id: str,
    ) -> ToolResult:
        assert self._store is not None
        if decision:
            logger.info("confirmation #{} approved by user={}", pending.id, user_id)
            self._store.mark(pending.id, status=STATUS_APPROVED)
            tool = self._registry.get(pending.tool_name)
            if tool is None:
                return ToolResult(ok=False, content="", error=f"tool '{pending.tool_name}' is no longer registered")
            try:
                tool_result = await tool.execute(pending.tool_arguments)
            except Exception as exc:  # noqa: BLE001
                logger.exception("resumed tool '{}' raised", pending.tool_name)
                tool_result = ToolResult(ok=False, content="", error=f"{type(exc).__name__}: {exc}")
            logger.info("resumed tool '{}' -> ok={} chars={}", pending.tool_name, tool_result.ok, len(tool_result.content))
            return tool_result
        logger.info("confirmation #{} denied by user={}", pending.id, user_id)
        self._store.mark(pending.id, status=STATUS_DENIED)
        return ToolResult(ok=False, content="", error=f"user denied execution of '{pending.tool_name}'")

    def _build_resume_outcome(
        self,
        *,
        pending: ConfirmationSnapshot,
        decision: bool,
        tool_result: ToolResult,
        direct_reply: str,
    ):
        from ..runtime import LoopOutcome
        status = "approved" if decision else "denied"
        outcome = LoopOutcome(
            final_text=direct_reply,
            tool_call_count=1 if decision else 0,
            invoked_tool_names=(pending.tool_name,) if decision else (),
            tool_outcomes=((pending.tool_name, bool(tool_result.ok), tool_result.error),),
        )
        self._memory_sync_fn(
            user_content=f"(confirmation {status}: {pending.tool_name})",
            assistant_content=direct_reply,
            session_id=f"{pending.platform}:{pending.user_id}",
            metadata={
                "platform": pending.platform,
                "user_id": pending.user_id,
                "interactive": True,
                "resume": True,
                "decision": status,
                "tool_name": pending.tool_name,
                "tool_outcomes": outcome.tool_outcomes,
                "invoked_tools": outcome.invoked_tool_names,
                "direct_confirmation_reply": True,
            },
            outcome=outcome,
        )
        return outcome

    def _resume_direct_reply(
        self,
        *,
        pending: ConfirmationSnapshot,
        message: IncomingMessage,
        decision: bool,
        tool_result: ToolResult,
        direct_reply: str,
        status: str,
    ) -> OutgoingMessage:
        assert self._store is not None
        self._store.mark(pending.id, status=STATUS_RESOLVED, outcome=status)
        self._build_resume_outcome(
            pending=pending,
            decision=decision,
            tool_result=tool_result,
            direct_reply=direct_reply,
        )
        return OutgoingMessage(target=message.reply_target, text=direct_reply)

    async def _resume_with_tool_loop(
        self,
        *,
        pending: ConfirmationSnapshot,
        message: IncomingMessage,
        tool_result: ToolResult,
        status: str,
    ) -> Optional[OutgoingMessage]:
        assert self._store is not None
        history: list[LLMMessage] = [LLMMessage.from_dict(d) for d in pending.history]
        _append_tool_message(tool_result, tool_call_id=pending.tool_call_id, tool_name=pending.tool_name, history=history)
        outcome = await self._run_tool_loop_fn(
            history,
            system=pending.system_prompt or self._system_prompt_dm,
            interactive=True,
            platform=pending.platform,
            user_id=pending.user_id,
            reply_target=message.reply_target,
            user_text_for_fallback="(continuation after confirmation)",
            session_id=f"{pending.platform}:{pending.user_id}",
        )
        self._store.mark(pending.id, status=STATUS_RESOLVED, outcome=status)
        self._memory_sync_fn(
            user_content=f"(confirmation {status}: {pending.tool_name})",
            assistant_content=outcome.final_text or "",
            session_id=f"{pending.platform}:{pending.user_id}",
            metadata={
                "platform": pending.platform,
                "user_id": pending.user_id,
                "interactive": True,
                "resume": True,
                "decision": status,
                "tool_name": pending.tool_name,
                "tool_outcomes": outcome.tool_outcomes,
                "invoked_tools": outcome.invoked_tool_names,
            },
            outcome=outcome,
        )
        return outcome.to_message(reply_target=message.reply_target)

    async def suspend(
        self,
        *,
        tc: "LLMToolCall",
        history: list[LLMMessage],
        platform: str,
        user_id: str,
        reply_target: DeliveryTarget,
        system: str,
    ) -> "LoopOutcome":
        assert self._store is not None
        arguments, _ = _parse_tool_call_args(tc)
        self._store.supersede_pending_for(platform=platform, user_id=user_id)

        deferred_history = list(history)
        last_asst = history[-1] if history and history[-1].role == "assistant" else None
        if last_asst is not None and last_asst.tool_calls and len(last_asst.tool_calls) > 1:
            for sibling in last_asst.tool_calls:
                if sibling.id == tc.id:
                    continue
                deferred_history.append(LLMMessage(
                    role="tool",
                    content=(
                        "[ZLAgent] Deferred: another tool call from this step is paused for user confirmation."
                        " If this operation is still needed after the user resolves that confirmation, re-emit it on the next turn."
                    ),
                    name=sibling.name,
                    tool_call_id=sibling.id,
                ))
            logger.info("freezing {} sibling tool_call(s) as deferred to keep history balanced", len(last_asst.tool_calls) - 1)

        question = _lc.format_confirmation_question(tc.name, arguments)
        snapshot = self._store.create(
            platform=platform, user_id=user_id,
            reply_target={"platform": reply_target.platform, "target_type": reply_target.target_type,
                          "target_id": reply_target.target_id, "display_name": reply_target.display_name},
            tool_name=tc.name, tool_arguments=arguments,
            tool_call_id=tc.id,
            history=[m.to_dict() for m in deferred_history],
            system_prompt=system,
            question_text=question,
        )
        logger.info("suspending agent loop on confirmation #{} (tool={!r})", snapshot.id, tc.name)
        from ..runtime import LoopOutcome
        return LoopOutcome(
            question_message=OutgoingMessage(target=reply_target, text=question),
            suspended_confirmation_id=snapshot.id,
        )
