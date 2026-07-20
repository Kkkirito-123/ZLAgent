from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from typing import Callable, Optional, TYPE_CHECKING

from loguru import logger

from ...core.redact import redact_mapping
from ...tools import ToolPermission, ToolResult
from ...tools.permission import PermissionDecision, PermissionPolicy
from ...llm import LLMMessage
from ..prompts import WEIXIN_DIRECT_DONE_CRON_ACTIONS
from ..recovery import append_toolguard_guidance, toolguard_synthetic_result
from . import types as _t

if TYPE_CHECKING:
    from ...llm import LLMToolCall
    from ...tools import ToolRegistry
    from ...harness.execution import HarnessExecution
    from ..recovery import ToolCallGuardrailController
    from ..context import ContextEngine
    from ..runtime import LoopOutcome

MAX_TOOL_MESSAGE_CHARS = 20_000


DEFAULT_PERMISSION_POLICY = PermissionPolicy()


def _parse_tool_call_args(tc: "LLMToolCall") -> tuple[dict, str]:
    try:
        args = json.loads(tc.arguments) if tc.arguments else {}
        if not isinstance(args, dict):
            raise json.JSONDecodeError("not a JSON object", tc.arguments, 0)
    except json.JSONDecodeError:
        logger.warning(
            "tool '{}' got non-JSON arguments (truncated): {!r}",
            tc.name, tc.arguments[:200],
        )
        args = {}
    return args, tc.arguments


def _log_tool_result(tc: "LLMToolCall", arguments: dict, result: ToolResult) -> None:
    safe_arguments = redact_mapping(arguments)
    if result.ok:
        logger.info("tool '{}' -> ok=True chars={} args={}", tc.name, len(result.content), safe_arguments)
    else:
        logger.warning("tool '{}' -> ok=False error={!r} args={}", tc.name, result.error, safe_arguments)


def _append_tool_message(
    result: ToolResult,
    *,
    tool_call_id: str,
    tool_name: str,
    history: list[LLMMessage],
    max_chars: int = MAX_TOOL_MESSAGE_CHARS,
) -> None:
    content = result.to_tool_message_content()
    max_chars = max(1, min(MAX_TOOL_MESSAGE_CHARS, max_chars))
    if len(content) > max_chars:
        content = content[:max_chars] + "\n[...truncated]"
    history.append(LLMMessage(role="tool", content=content, name=tool_name, tool_call_id=tool_call_id))


def _direct_completion_reply(
    platform: str,
    prepared: list["_t.PreparedToolCall"],
    new_outcomes: list[tuple[str, bool, Optional[str]]],
) -> Optional[str]:
    if platform != "weixin":
        return None
    should_finish = any(
        item.tc.name == "cron_manage"
        and str(item.arguments.get("action") or "").strip().lower() in WEIXIN_DIRECT_DONE_CRON_ACTIONS
        for item in prepared
    )
    if not should_finish:
        return None
    for name, ok, error in new_outcomes:
        if name == "cron_manage" and not ok:
            fallback = error or "\u672a\u77e5\u9519\u8bef"
            return f"\u6267\u884c\u5931\u8d25\uff1a{fallback}"
    from ..confirmation import formatting as _lc
    cron_item = next(
        (item for item in prepared
         if item.tc.name == "cron_manage"
         and str(item.arguments.get("action") or "").strip().lower() in WEIXIN_DIRECT_DONE_CRON_ACTIONS),
        None,
    )
    if cron_item is not None:
        return _lc.direct_cron_reply(cron_item.arguments, ToolResult(ok=True, content=""))
    return "\u5df2\u5b8c\u6210\u3002"


def _can_run_parallel(item: "_t.PreparedToolCall") -> bool:
    tool = item.tool
    return bool(
        tool is not None
        and tool.permission is ToolPermission.SAFE
        and tool.is_read_only
        and tool.is_concurrency_safe
    )


class ToolLoopRunner:
    def __init__(
        self,
        *,
        registry: Optional["ToolRegistry"],
        guardrails: Optional["ToolCallGuardrailController"],
        context_engine: "ContextEngine",
        max_iterations: int,
        trajectory_compress: bool,
        suspend_fn: Optional[Callable] = None,
        parallel_max_concurrency: int = 4,
        permission_policy: Optional[PermissionPolicy] = None,
        execution: Optional["HarnessExecution"] = None,
    ) -> None:
        self._registry = registry
        self._guardrails = guardrails
        self._context_engine = context_engine
        self._max_iterations = max_iterations
        self._trajectory_compress = trajectory_compress
        self._suspend_fn = suspend_fn
        self._parallel_max_concurrency = max(1, int(parallel_max_concurrency))
        self._permission_policy = permission_policy or DEFAULT_PERMISSION_POLICY
        self._execution = execution

    async def run(
        self,
        history: list[LLMMessage],
        *,
        llm,
        system: str,
        interactive: bool,
        platform: str,
        user_id: str,
        reply_target,
        user_text_for_fallback: str,
        tool_whitelist: Optional[set[str]] = None,
        trust_confirm_tools: bool = False,
        max_iterations: Optional[int] = None,
        action_filter: Optional[Callable[[str, dict], Optional[str]]] = None,
        session_id: Optional[str] = None,
    ) -> "LoopOutcome":
        assert llm is not None

        control = _t.ToolLoopControlState.build(
            default_budget=self._max_iterations,
            max_iterations=max_iterations,
            interactive=interactive,
            trust_confirm_tools=trust_confirm_tools,
        )

        if self._guardrails is not None:
            self._guardrails.reset_for_turn()

        for step in range(control.budget):
            await self._maybe_compress(history)
            tools_schema = (
                self._registry.to_openai_schema(
                    include_confirm=control.expose_confirm,
                    whitelist=tool_whitelist,
                    force_include=control.activated_deferred_tools,
                )
                if self._registry
                else None
            )
            tool_choice = "auto" if tools_schema else None
            try:
                llm_started = time.perf_counter()
                response = await llm.chat(
                    history, tools=tools_schema, tool_choice=tool_choice, session_id=session_id,
                )
                logger.info(
                    "[perf] llm.chat session={} step={}/{} tools={} elapsed_ms={}",
                    session_id or "", step + 1, control.budget,
                    len(tools_schema or []),
                    int((time.perf_counter() - llm_started) * 1000),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("LLM call failed, falling back to verbatim text: {}", exc)
                return control.outcome(
                    "模型暂时连不上，稍后再试一下。",
                    failure_kind="llm_error",
                )

            if not response.tool_calls:
                if response.content:
                    return control.outcome(response.content)
                return control.outcome(
                    "模型这一回没产生回复，可以换个问法或稍后重试。",
                    failure_kind="empty_response",
                )

            logger.info("agent step {}/{}: {} tool call(s)", step + 1, control.budget, len(response.tool_calls))
            history.append(LLMMessage(
                role="assistant",
                content=response.content or "",
                tool_calls=response.tool_calls,
                reasoning_content=response.reasoning_content,
            ))
            control.invoked_tool_names.extend(tc.name for tc in response.tool_calls)

            pending_confirm = self._first_confirm_call(
                response.tool_calls, interactive=interactive,
                trust_confirm_tools=trust_confirm_tools, platform=platform,
            )
            if pending_confirm is not None and self._suspend_fn is not None and reply_target is not None:
                return await self._suspend_fn(
                    tc=pending_confirm, history=history,
                    platform=platform, user_id=user_id,
                    reply_target=reply_target, system=system,
                )

            prepared = self._prepare_calls(
                response.tool_calls,
                interactive=interactive, trust_confirm_tools=trust_confirm_tools,
                platform=platform, history=history,
                tool_outcomes=control.tool_outcomes, action_filter=action_filter,
            )
            outcome_start = len(control.tool_outcomes)
            await self._execute_prepared(
                prepared, history=history,
                tool_outcomes=control.tool_outcomes,
                activated_deferred_tools=control.activated_deferred_tools,
                session_id=session_id,
            )
            direct_reply = _direct_completion_reply(
                platform,
                prepared,
                control.tool_outcomes[outcome_start:],
            )
            if direct_reply is not None:
                return control.outcome(direct_reply)

            if (
                self._guardrails is not None
                and self._guardrails.halt_decision is not None
                and self._guardrails.halt_decision.should_halt
            ):
                halt = self._guardrails.halt_decision
                logger.warning("[guardrail] halting loop after step {} ({}: {})", step + 1, halt.code, halt.message)
                break

        logger.warning("agent hit max_tool_iterations={}, forcing final answer", control.budget)
        await self._maybe_compress(history)
        # Ephemeral nudge to keep the final pass in natural language.
        final_history: list[LLMMessage] = list(history)
        final_history.append(
            LLMMessage(
                role="user",
                content=(
                    "【系统收尾】本回合已达到工具步数上限；请只用自然语言"
                    "一两段总结进度与结论。禁止输出 XML、DSML、"
                    "`<function_calls>` 或任何工具调用标记。"
                ),
            ),
        )
        try:
            llm_started = time.perf_counter()
            final = await llm.chat(
                final_history,
                tools=None,
                session_id=session_id,
                stream=False,
            )
            logger.info(
                "[perf] llm.chat session={} final=true elapsed_ms={}",
                session_id or "", int((time.perf_counter() - llm_started) * 1000),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("final LLM call failed: {}", exc)
            return control.outcome(
                "模型暂时连不上，稍后再试一下。",
                failure_kind="llm_error",
            )
        if final.content:
            return control.outcome(final.content)
        return control.outcome(
            "任务步骤太多没收敛，先到这里。再试一下、或把问题拆小一点。",
            failure_kind="max_iterations",
        )

    def _first_confirm_call(
        self,
        calls: list["LLMToolCall"],
        *,
        interactive: bool,
        trust_confirm_tools: bool,
        platform: str,
    ) -> Optional["LLMToolCall"]:
        if not interactive or trust_confirm_tools or self._registry is None:
            return None
        for tc in calls:
            tool = self._registry.get(tc.name)
            arguments, _ = _parse_tool_call_args(tc)
            decision = self._permission_policy.decide_tool_call(
                tool=tool,
                tool_name=tc.name,
                arguments=arguments,
                platform=platform,
                interactive=interactive,
                trust_confirm_tools=trust_confirm_tools,
            )
            if decision.decision is PermissionDecision.CONFIRM:
                return tc
        return None

    def _prepare_calls(
        self,
        calls: list["LLMToolCall"],
        *,
        interactive: bool,
        trust_confirm_tools: bool,
        platform: str,
        history: list[LLMMessage],
        tool_outcomes: list[tuple[str, bool, Optional[str]]],
        action_filter: Optional[Callable[[str, dict], Optional[str]]],
    ) -> list["_t.PreparedToolCall"]:
        prepared: list[_t.PreparedToolCall] = []
        for tc in calls:
            tool = self._registry.get(tc.name) if self._registry else None
            arguments, _ = _parse_tool_call_args(tc)
            if action_filter is not None:
                veto = action_filter(tc.name, arguments)
                if veto is not None:
                    logger.warning("action_filter vetoed tool '{}': {}", tc.name, veto)
                    _append_tool_message(
                        ToolResult(ok=False, content="", error=veto),
                        tool_call_id=tc.id, tool_name=tc.name, history=history,
                    )
                    continue

            permission = self._permission_policy.decide_tool_call(
                tool=tool,
                tool_name=tc.name,
                arguments=arguments,
                platform=platform,
                interactive=interactive,
                trust_confirm_tools=trust_confirm_tools,
            )
            if permission.decision is PermissionDecision.DENY:
                logger.warning("permission policy denied tool '{}': {}", tc.name, permission.reason)
                _append_tool_message(
                    ToolResult(ok=False, content="", error=permission.reason),
                    tool_call_id=tc.id, tool_name=tc.name, history=history,
                )
                continue

            if self._guardrails is not None:
                g_decision = self._guardrails.before_call(
                    tc.name, arguments,
                    is_read_only=bool(tool is not None and tool.is_read_only),
                )
                if not g_decision.allows_execution:
                    logger.warning("[guardrail] blocked tool '{}' code={} count={}", tc.name, g_decision.code, g_decision.count)
                    synthetic = toolguard_synthetic_result(g_decision)
                    _append_tool_message(
                        ToolResult(ok=True, content=synthetic),
                        tool_call_id=tc.id, tool_name=tc.name, history=history,
                    )
                    tool_outcomes.append((tc.name, False, g_decision.message or g_decision.code))
                    continue

            max_chars = MAX_TOOL_MESSAGE_CHARS
            if tool is not None:
                max_chars = min(MAX_TOOL_MESSAGE_CHARS, max(1, tool.max_result_chars))
            prepared.append(_t.PreparedToolCall(
                tc=tc, arguments=arguments, tool=tool,
                max_result_chars=max_chars,
                allow_confirm=permission.allow_confirm,
            ))
        return prepared

    async def _execute_prepared(
        self,
        prepared: list["_t.PreparedToolCall"],
        *,
        history: list[LLMMessage],
        tool_outcomes: list[tuple[str, bool, Optional[str]]],
        activated_deferred_tools: set[str],
        session_id: Optional[str],
    ) -> None:
        if not prepared:
            return

        # Execute parallel-safe tools concurrently, then serial tools in order.
        parallel_indices: list[int] = [
            i for i, item in enumerate(prepared) if _can_run_parallel(item)
        ]
        parallel_tasks: dict[int, asyncio.Task[ToolResult]] = {}
        batch_started: Optional[float] = None
        concurrency = 1

        if len(parallel_indices) >= 2:
            concurrency = min(self._parallel_max_concurrency, len(parallel_indices))
            sema = asyncio.Semaphore(concurrency)

            async def _gated(item: "_t.PreparedToolCall") -> ToolResult:
                async with sema:
                    return await self._call_one(item, session_id=session_id)

            batch_started = time.perf_counter()
            for i in parallel_indices:
                parallel_tasks[i] = asyncio.create_task(_gated(prepared[i]))
            logger.info(
                "[perf] tool.parallel kickoff batch_size={} concurrency={}",
                len(parallel_indices), concurrency,
            )

        for i, item in enumerate(prepared):
            task = parallel_tasks.get(i)
            if task is not None:
                result = await task
            else:
                result = await self._call_one(item, session_id=session_id)
            self._record_result(
                item, result,
                history=history, tool_outcomes=tool_outcomes,
                activated_deferred_tools=activated_deferred_tools,
            )

        if parallel_tasks and batch_started is not None:
            elapsed = int((time.perf_counter() - batch_started) * 1000)
            logger.info(
                "[perf] tool.parallel done batch_size={} concurrency={} elapsed_ms={}",
                len(parallel_tasks), concurrency, elapsed,
            )

    async def _call_one(
        self,
        item: "_t.PreparedToolCall",
        *,
        session_id: Optional[str],
    ) -> ToolResult:
        assert self._execution is not None
        started = time.perf_counter()
        try:
            return await self._execution.execute(
                item.tc.name,
                item.arguments,
                allow_confirm=item.allow_confirm,
                session_id=session_id,
            )
        finally:
            logger.info("[perf] tool name={} elapsed_ms={}", item.tc.name, int((time.perf_counter() - started) * 1000))

    def _record_result(
        self,
        item: "_t.PreparedToolCall",
        result: ToolResult,
        *,
        history: list[LLMMessage],
        tool_outcomes: list[tuple[str, bool, Optional[str]]],
        activated_deferred_tools: set[str],
    ) -> None:
        tc = item.tc
        tool_outcomes.append((tc.name, bool(result.ok), result.error))
        _log_tool_result(tc, item.arguments, result)
        self._activate_deferred(result, activated_deferred_tools)
        if self._guardrails is not None:
            decision = self._guardrails.after_call(
                tc.name, item.arguments, result.content,
                failed=not result.ok,
                is_read_only=bool(item.tool is not None and item.tool.is_read_only),
            )
            if decision.action in {"warn", "halt"} and decision.message:
                logger.info("[guardrail] {} tool '{}' code={} count={}", decision.action, tc.name, decision.code, decision.count)
                result = replace(
                    result,
                    content=append_toolguard_guidance(result.content, decision),
                )
        _append_tool_message(result, tool_call_id=tc.id, tool_name=tc.name, history=history, max_chars=item.max_result_chars)

    def _activate_deferred(self, result: ToolResult, activated_deferred_tools: set[str]) -> None:
        raw = result.raw or {}
        names = raw.get("activate_tools")
        if not isinstance(names, list) or self._registry is None:
            return
        for name in names:
            if isinstance(name, str) and self._registry.get(name) is not None:
                activated_deferred_tools.add(name)

    async def _maybe_compress(self, history: list[LLMMessage]) -> None:
        if not self._trajectory_compress:
            return
        stats = await self._context_engine.maybe_compress_async(history)
        if stats.compressed:
            logger.info(
                "[context] compressed {} \u2192 {} chars (\u2248{} \u2192 \u2248{} tokens,"
                " {} \u2192 {} msgs, phase={}, rounds_dropped={},"
                " tools_truncated={}, micro_compacted={},"
                " summary_used={}, summary_chars={})",
                stats.before_chars, stats.after_chars,
                stats.before_tokens, stats.after_tokens,
                stats.before_messages, stats.after_messages,
                stats.phase, stats.rounds_dropped,
                stats.tool_messages_truncated, stats.micro_compacted_messages,
                stats.summary_used, stats.summary_chars,
            )
