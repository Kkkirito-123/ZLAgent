from __future__ import annotations

import asyncio
from typing import Any, Optional, TYPE_CHECKING

from loguru import logger

from ..recovery import detect_correction, render_review_hint
from ..routing.inheritance import is_confirmation_only
from ..prompts import (
    END_OF_DAY_REVIEW_FORBIDDEN_SKILL_ACTIONS,
    END_OF_DAY_REVIEW_PROMPT,
    REVIEW_ALLOWED_MEMORY_ACTIONS,
    REVIEW_FORBIDDEN_SKILL_ACTIONS,
    SKILL_REVIEW_PROMPT,
)
from ...core.provenance import BACKGROUND_REVIEW, reset_current_write_origin, set_current_write_origin
from ...memory.intent import render_memory_intent_review_hint
from ...llm import LLMMessage

if TYPE_CHECKING:
    from ...memory.intent import MemoryIntentSignal
    from ...memory.manager import MemoryManager
    from ...skills.loader import SkillManifest
    from ...tools import ToolRegistry
    from ..recovery import CorrectionSignal, FailureLearner
    from ..runtime import LoopOutcome
    from ..tool_loop import ToolLoopRunner


class PostTurnPipeline:
    def __init__(
        self,
        *,
        memory: Optional["MemoryManager"] = None,
        failure_learner: Optional["FailureLearner"] = None,
        wiki_store: Optional[Any] = None,
        crystallizer: Optional[Any] = None,
        tool_loop_runner: Optional["ToolLoopRunner"] = None,
        registry: Optional["ToolRegistry"] = None,
        review_enabled: bool = True,
        review_min_steps: int = 2,
        review_max_iterations: int = 3,
    ) -> None:
        self._memory = memory
        self._failure_learner = failure_learner
        self._wiki_store = wiki_store
        self._crystallizer = crystallizer
        self._tool_loop_runner = tool_loop_runner
        self._registry = registry
        self._review_enabled = review_enabled
        self._review_min_steps = review_min_steps
        self._review_max_iterations = review_max_iterations
        self._pending_wiki_writes: set[asyncio.Task[None]] = set()
        self._pending_crystallize: set[asyncio.Task[None]] = set()
        self._pending_reviews: set[asyncio.Task[None]] = set()

    def sync_memory_turn(
        self,
        *,
        user_content: str,
        assistant_content: str,
        session_id: str,
        metadata: dict,
        outcome: Optional["LoopOutcome"],
    ) -> None:
        if self._memory is None:
            return
        if outcome is not None and outcome.suspended_confirmation_id is not None:
            return
        try:
            self._memory.sync_turn(user_content, assistant_content, session_id=session_id, metadata=metadata)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[memory] turn sync failed: {}", exc)

    def handle(
        self,
        *,
        outcome: "LoopOutcome",
        safe_text: str,
        session_id: str,
        platform: str,
        user_id: str,
        invoked_skill_id: Optional[str],
        invoked_skill_manifest: Optional["SkillManifest"],
        history: "list[LLMMessage]",
        memory_intent: Optional["MemoryIntentSignal"],
        llm: Any,
    ) -> None:
        if (
            self._failure_learner is not None
            and outcome.suspended_confirmation_id is None
            and outcome.tool_outcomes
        ):
            self._failure_learner.observe_turn(outcome.tool_outcomes)

        correction = detect_correction(safe_text)
        if correction is not None:
            logger.info(
                "[turn] correction detected (confidence={}, phrase={!r})",
                correction.confidence.value, correction.phrase,
            )
        self._schedule_review(
            outcome=outcome, history=history, platform=platform,
            user_id=user_id, correction=correction, memory_intent=memory_intent, llm=llm,
        )
        self.sync_memory_turn(
            user_content=safe_text,
            assistant_content=outcome.final_text or "",
            session_id=session_id,
            metadata={
                "platform": platform,
                "user_id": user_id,
                "interactive": True,
                "tool_outcomes": outcome.tool_outcomes,
                "invoked_tools": outcome.invoked_tool_names,
                "skill_hint": invoked_skill_id or "",
            },
            outcome=outcome,
        )
        if (
            self._wiki_store is not None
            and invoked_skill_id is not None
            and invoked_skill_manifest is not None
            and getattr(invoked_skill_manifest, "wiki_cache_enabled", False)
            and outcome.final_text
            and not outcome.invoked_tool_names
            and outcome.suspended_confirmation_id is None
            and not is_confirmation_only(safe_text)
        ):
            self._schedule_wiki_write(
                skill_id=invoked_skill_id,
                raw_query=safe_text,
                answer=outcome.final_text,
                ttl_seconds=getattr(invoked_skill_manifest, "wiki_cache_ttl_seconds", None),
                metadata={
                    "platform": platform,
                    "skill_version": getattr(invoked_skill_manifest, "version", ""),
                    "model": getattr(llm, "model", "") if llm else "",
                },
            )
        if (
            self._crystallizer is not None
            and invoked_skill_id is not None
            and invoked_skill_manifest is not None
            and getattr(invoked_skill_manifest, "crystallize", False)
            and outcome.final_text
            and not outcome.invoked_tool_names
            and outcome.suspended_confirmation_id is None
            and not is_confirmation_only(safe_text)
        ):
            self._schedule_crystallize(
                skill_id=invoked_skill_id,
                answer=outcome.final_text,
                ttl_seconds=getattr(invoked_skill_manifest, "wiki_cache_ttl_seconds", None),
            )

    def _should_trigger_review(
        self,
        outcome: "LoopOutcome",
        *,
        llm: Any,
        correction: Optional["CorrectionSignal"] = None,
        memory_intent: Optional["MemoryIntentSignal"] = None,
    ) -> bool:
        if not self._review_enabled:
            return False
        if not (llm and getattr(llm, "configured", False)):
            return False
        if self._registry is None or self._registry.get("skill_manage") is None:
            return False
        if outcome.suspended_confirmation_id is not None:
            return False
        if outcome.tool_call_count >= self._review_min_steps:
            return True
        if memory_intent is not None and memory_intent.triggered:
            if memory_intent.confidence == "high":
                return True
            if memory_intent.confidence == "medium" and outcome.tool_call_count == 0:
                return True
        if correction is not None:
            from ..recovery import CorrectionConfidence as _Conf
            if correction.confidence is _Conf.HIGH:
                return True
            if correction.confidence is _Conf.MEDIUM and outcome.tool_call_count >= 1:
                return True
        return False

    @staticmethod
    def _review_action_filter(
        *,
        tool_name: str,
        arguments: dict,
        forbidden_skill_actions: set[str],
        skill_scope_label: str,
    ) -> Optional[str]:
        action = str(arguments.get("action") or "").strip().lower()
        if tool_name == "skill_manage":
            if action in forbidden_skill_actions:
                return (
                    f"action={action!r} is forbidden during {skill_scope_label};"
                    " destructive skill actions are reserved for the"
                    " end-of-day review / operator confirmation."
                )
            return None
        if tool_name == "memory_manage":
            if action not in REVIEW_ALLOWED_MEMORY_ACTIONS:
                return (
                    f"action={action!r} is forbidden during review turns;"
                    " review may only ``remember`` / ``recall`` / ``list`` /"
                    " ``consolidate``."
                )
            return None
        return f"tool {tool_name!r} is not allowed during a review turn"

    @staticmethod
    def review_action_filter(tool_name: str, arguments: dict) -> Optional[str]:
        return PostTurnPipeline._review_action_filter(
            tool_name=tool_name,
            arguments=arguments,
            forbidden_skill_actions=REVIEW_FORBIDDEN_SKILL_ACTIONS,
            skill_scope_label="intra-day review",
        )

    @staticmethod
    def end_of_day_review_action_filter(
        tool_name: str, arguments: dict,
    ) -> Optional[str]:
        return PostTurnPipeline._review_action_filter(
            tool_name=tool_name,
            arguments=arguments,
            forbidden_skill_actions=END_OF_DAY_REVIEW_FORBIDDEN_SKILL_ACTIONS,
            skill_scope_label="the end-of-day review",
        )

    @staticmethod
    def summarize_main_turn(history: "list[LLMMessage]") -> str:
        lines: list[str] = []
        for msg in history:
            if msg.role == "system":
                continue
            if msg.role == "user":
                lines.append(f"[user] {msg.content[:800]}")
            elif msg.role == "assistant":
                head = (msg.content or "").strip()
                if head:
                    lines.append(f"[assistant] {head[:800]}")
                if msg.tool_calls:
                    for tc in msg.tool_calls:
                        args_preview = (tc.arguments or "")[:300]
                        lines.append(f"[assistant\u2192tool] {tc.name}({args_preview})")
            elif msg.role == "tool":
                preview = (msg.content or "")[:600]
                lines.append(f"[tool:{msg.name or '?'}] {preview}")
        return "\n".join(lines) if lines else "(empty turn)"

    async def _run_review_turn(
        self,
        *,
        llm: Any,
        main_turn_history: "list[LLMMessage]",
        platform: str,
        user_id: str,
        correction: Optional["CorrectionSignal"] = None,
        memory_intent: Optional["MemoryIntentSignal"] = None,
    ) -> None:
        token = set_current_write_origin(BACKGROUND_REVIEW)
        try:
            assert llm is not None
            summary = self.summarize_main_turn(main_turn_history)
            review_system = SKILL_REVIEW_PROMPT
            if self._memory is not None:
                snapshot = self._memory.system_prompt_block()
                if snapshot:
                    review_system = f"{SKILL_REVIEW_PROMPT}\n\n{snapshot}"
            user_block = (
                "\u4e0b\u9762\u662f\u521a\u521a\u5b8c\u6210\u7684\u4e3b\u8f6e\u6b21\u7684\u5b8c\u6574\u7ecf\u8fc7\uff0c\u8bf7\u6309 system prompt"
                " \u8bc4\u4f30\u5e76\u51b3\u5b9a\u662f\u5426\u8c03\u7528 skill_manage\uff1a\n\n" + summary
            )
            if correction is not None:
                user_block = user_block + render_review_hint(correction)
                logger.info(
                    "[skill review] correction signal injected (confidence={}, phrase={!r})",
                    correction.confidence.value, correction.phrase,
                )
            if memory_intent is not None and memory_intent.triggered:
                user_block = user_block + render_memory_intent_review_hint(memory_intent)
                logger.info(
                    "[skill review] memory intent injected (confidence={}, matched={})",
                    memory_intent.confidence, ",".join(memory_intent.matched),
                )
            review_history = [
                LLMMessage(role="system", content=review_system),
                LLMMessage(role="user", content=user_block),
            ]
            assert self._tool_loop_runner is not None
            outcome = await self._tool_loop_runner.run(
                review_history,
                llm=llm,
                system=review_system,
                interactive=False,
                platform=platform,
                user_id=user_id,
                reply_target=None,
                user_text_for_fallback="",
                tool_whitelist={"skill_manage", "memory_manage"},
                trust_confirm_tools=True,
                max_iterations=self._review_max_iterations,
                action_filter=self.review_action_filter,
                session_id=f"review:{platform}:{user_id}",
            )
            invoked = [
                name
                for name in outcome.invoked_tool_names
                if name in {"skill_manage", "memory_manage"}
            ]
            tail = (outcome.final_text or "").strip()[:200]
            if invoked:
                logger.info("[skill review] invoked={}; final: {!r}", invoked, tail)
            else:
                logger.info("[skill review] no-op ({!r})", tail)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[skill review] turn failed: {}", exc)
        finally:
            reset_current_write_origin(token)

    async def run_end_of_day_review(
        self,
        *,
        llm: Any,
        user_block: str,
        max_iterations: int = 8,
    ) -> dict[str, Any]:
        """Run the **end-of-day** review fork (Phase B).

        Public sibling of :meth:`_run_review_turn` — this is the path
        that ``DailyReviewService`` calls every 24 h (or that an
        operator triggers manually via REST). Differences from the
        intra-day path:

        * Uses :data:`END_OF_DAY_REVIEW_PROMPT` as the system prompt.
        * Uses :meth:`end_of_day_review_action_filter` so
          ``skill_manage`` create / edit / patch are now allowed
          (delete / remove_file remain blocked).
        * Default ``max_iterations`` is higher (8) — this is the heavy
          pass that may legitimately rewrite multiple skills + run
          multiple ``memory_manage(consolidate)`` calls.
        * Caller pre-builds ``user_block`` (the daily service stuffs
          past-24h memory + skill stats in there). We don't summarise
          a recent main turn here.
        * Returns a structured dict instead of swallowing the result —
          the daily service stores it as ``last_summary`` so REST /
          IM can show "今天新增 N skills + M memory writes".
        """
        if llm is None or not getattr(llm, "configured", False):
            return {
                "ok": False,
                "reason": "llm not configured",
                "skill_calls": 0,
                "memory_calls": 0,
                "final_text": "",
                "tool_outcomes": [],
            }
        if self._tool_loop_runner is None:
            return {
                "ok": False,
                "reason": "tool_loop_runner not wired",
                "skill_calls": 0,
                "memory_calls": 0,
                "final_text": "",
                "tool_outcomes": [],
            }
        token = set_current_write_origin(BACKGROUND_REVIEW)
        try:
            review_system = END_OF_DAY_REVIEW_PROMPT
            if self._memory is not None:
                snap = self._memory.system_prompt_block()
                if snap:
                    review_system = f"{END_OF_DAY_REVIEW_PROMPT}\n\n{snap}"
            review_history = [
                LLMMessage(role="system", content=review_system),
                LLMMessage(role="user", content=user_block),
            ]
            outcome = await self._tool_loop_runner.run(
                review_history,
                llm=llm,
                system=review_system,
                interactive=False,
                platform="cron",
                user_id="daily_review",
                reply_target=None,
                user_text_for_fallback="",
                tool_whitelist={"skill_manage", "memory_manage"},
                trust_confirm_tools=True,
                max_iterations=max_iterations,
                action_filter=self.end_of_day_review_action_filter,
                session_id="review:end-of-day:daily",
            )
            skill_calls = sum(
                1 for n in outcome.invoked_tool_names if n == "skill_manage"
            )
            memory_calls = sum(
                1 for n in outcome.invoked_tool_names if n == "memory_manage"
            )
            final_text = (outcome.final_text or "").strip()
            logger.info(
                "[end-of-day review] skill_calls={} memory_calls={}"
                " final={!r}",
                skill_calls, memory_calls, final_text[:200],
            )
            return {
                "ok": True,
                "skill_calls": skill_calls,
                "memory_calls": memory_calls,
                "final_text": final_text[:600],
                # tool_outcomes is a tuple of (name, ok, error) tuples
                # — flatten to dicts so REST/JSON can render it.
                "tool_outcomes": [
                    {"name": n, "ok": bool(ok), "error": err}
                    for (n, ok, err) in outcome.tool_outcomes
                ],
            }
        except Exception as exc:  # noqa: BLE001
            logger.exception("[end-of-day review] turn failed: {}", exc)
            return {
                "ok": False,
                "reason": str(exc),
                "skill_calls": 0,
                "memory_calls": 0,
                "final_text": "",
                "tool_outcomes": [],
            }
        finally:
            reset_current_write_origin(token)

    def _schedule_review(
        self,
        *,
        outcome: "LoopOutcome",
        history: "list[LLMMessage]",
        platform: str,
        user_id: str,
        llm: Any,
        correction: Optional["CorrectionSignal"] = None,
        memory_intent: Optional["MemoryIntentSignal"] = None,
    ) -> None:
        if not self._should_trigger_review(outcome, llm=llm, correction=correction, memory_intent=memory_intent):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(
            self._run_review_turn(
                llm=llm,
                main_turn_history=list(history),
                platform=platform, user_id=user_id,
                correction=correction, memory_intent=memory_intent,
            ),
            name=f"zlagent-review-{user_id or 'cron'}",
        )
        self._pending_reviews.add(task)
        task.add_done_callback(self._pending_reviews.discard)

    def _schedule_wiki_write(
        self,
        *,
        skill_id: str,
        raw_query: str,
        answer: str,
        ttl_seconds: Optional[int],
        metadata: dict,
    ) -> None:
        if self._wiki_store is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            try:
                self._wiki_store.add(skill_id, raw_query, answer, ttl_seconds=ttl_seconds, metadata=metadata)
            except Exception as exc:  # noqa: BLE001
                logger.warning("wiki write (sync fallback) failed skill={} err={}", skill_id, exc)
            return

        wiki_store = self._wiki_store

        async def _writer() -> None:
            try:
                await loop.run_in_executor(
                    None,
                    lambda: wiki_store.add(skill_id, raw_query, answer, ttl_seconds=ttl_seconds, metadata=metadata),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("wiki write failed skill={} err={}", skill_id, exc)

        task = loop.create_task(_writer())
        self._pending_wiki_writes.add(task)
        task.add_done_callback(self._pending_wiki_writes.discard)

    def _schedule_crystallize(self, *, skill_id: str, answer: str, ttl_seconds: Optional[int]) -> None:
        if self._crystallizer is None or not (answer and answer.strip()):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("[crystallize] no running loop, skipping skill={}", skill_id)
            return

        crystallizer = self._crystallizer

        async def _runner() -> None:
            try:
                await crystallizer.crystallize(skill_id=skill_id, answer=answer, ttl_seconds=ttl_seconds)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[crystallize] task failed skill={} err={}", skill_id, exc)

        task = loop.create_task(_runner())
        self._pending_crystallize.add(task)
        task.add_done_callback(self._pending_crystallize.discard)

    async def shutdown(self) -> None:
        for task_set in (self._pending_wiki_writes, self._pending_crystallize, self._pending_reviews):
            for task in tuple(task_set):
                task.cancel()
            if task_set:
                await asyncio.gather(*task_set, return_exceptions=True)
