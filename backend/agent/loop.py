"""Agent loop: turn an IncomingMessage / cron trigger into a reply via LLM.

This module is now a thin orchestrator. The actual work is decomposed into
single-responsibility collaborators (``TurnPreparer``, ``ToolLoopRunner``,
``PostTurnPipeline``, ``ConfirmationFlow``, ``CronRunner``,
``run_streaming_skill``) under ``backend/agent/``. ``AgentLoop`` wires them
together and exposes the public ``run_turn`` / ``generate_cron_message``
entry points used by gateways and the cron scheduler.

The core protocol implemented by ``ToolLoopRunner`` is the OpenAI
function-calling loop:

    1. Send the conversation + tools schema to the LLM.
    2. If the model emits ``tool_calls``:
       a. ``safe`` tools execute immediately, results are appended as
          ``role=tool`` messages, and the loop continues.
       b. The first ``confirm`` tool encountered (in *interactive* turns)
          suspends the loop: the agent persists a ``PendingConfirmation``
          row, sends a yes/no question to the user, and exits the turn.
          A subsequent inbound message classified as yes/no by
          ``backend.agent.confirmation.classify_decision`` resumes the loop
          with the user's verdict baked in as the tool's return value.
       c. ``confirm`` tools in *non-interactive* turns (cron triggers) are
          auto-denied with a synthetic error result so the model can adapt.
    3. Repeat until the model returns plain assistant content, hits
       ``max_tool_iterations``, or suspends on a confirmation.

The tool subsystem is orthogonal to the LLM: if either is missing we fall
back to a verbatim echo so the gateway never goes dark.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Callable, Optional

from loguru import logger

if TYPE_CHECKING:
    from pathlib import Path  # noqa: F401
    # WikiStore / Crystallizer / GeoStore / RouterLLM are annotation-only.
    # Pull them in via TYPE_CHECKING so deployments that explicitly disable
    # these subsystems don't pay the import cost or trip on missing tables.
    from ..wiki.store import WikiStore  # noqa: F401
    from ..wiki.crystallizer import Crystallizer  # noqa: F401
    from ..wiki.geo_store import GeoStore  # noqa: F401
    from .routing.llm_router import RouterLLM, RouterDecision  # noqa: F401
    from ..harness.observability.tracer import TraceRecorder

from ..db.confirmations import ConfirmationStore
from ..gateways.base import DeliveryTarget, IncomingMessage, OutgoingMessage
from ..llm import LLMClient, LLMMessage
from ..memory.intent import MemoryIntentSignal
from ..memory.manager import MemoryManager
from ..core.redact import redact_text
from ..harness.execution import HarnessExecution
from ..harness.observability.tracer import TurnRecord
from ..tools.permission import PermissionPolicy
from .recovery import CorrectionSignal, FailureLearner, ToolCallGuardrailController
from ..skills.loader import SkillLoader, SkillManifest
from ..tools import ToolRegistry
from .context import TurnContext, set_turn_context
from .context import ContextEngine, SummaryCompressor
from .runtime import LoopOutcome
from .runtime import adaptive_tool_budget as _adaptive_tool_budget
from .context import (
    DEFAULT_KEEP_LAST_TOOL_RESULTS,
    DEFAULT_KEEP_RECENT_ROUNDS,
    DEFAULT_MAX_CHARS,
    DEFAULT_TRUNCATED_TOOL_CHARS,
)

# Re-exports preserved for external callers (smoke tests, app.py, etc.).
# The canonical home is ``agent.prompts``; ``loop`` keeps these visible so
# legacy ``from backend.agent.loop import SYSTEM_PROMPT_DM`` still works.
from .prompts import (  # noqa: F401
    CONTROL_MEMORY_PRINCIPLES,
    KARPATHY_CODING_PRINCIPLES,
    REVIEW_ALLOWED_MEMORY_ACTIONS,
    REVIEW_FORBIDDEN_SKILL_ACTIONS,
    SILENT_MARKER,
    SKILL_REVIEW_PROMPT,
    SYSTEM_PROMPT_CRON,
    SYSTEM_PROMPT_DM,
)
from .routing import TurnRoutingPolicy
from .retrieval import WikiRetrievalPolicy
from .tool_loop import ToolLoopRunner
from .post_turn import PostTurnPipeline
from .confirmation import ConfirmationFlow
from .streaming import run_streaming_skill
from .cron_runner import CronRunner, build_cron_system_prompt, compose_cron_user_message
from .turn_preparer import TurnPreparer
from .confirmation import formatting as _lc  # noqa: E402


class AgentLoop:
    """Coordinates a single turn (inbound message or cron-triggered task)."""

    def __init__(
        self,
        llm: Optional[LLMClient] = None,
        *,
        tool_registry: Optional[ToolRegistry] = None,
        confirmation_store: Optional[ConfirmationStore] = None,
        skill_loader: Optional[SkillLoader] = None,
        memory_manager: Optional[MemoryManager] = None,
        failure_learner: Optional[FailureLearner] = None,
        tool_guardrails: Optional[ToolCallGuardrailController] = None,
        max_tool_iterations: int = 6,
        # v0.45 — adaptive cap for list-collection asks. When the
        # ``_is_list_task`` heuristic matches the user's message (find
        # N papers, comparison, survey, ...) the runner uses this
        # higher budget for the turn. Default 10; -1 disables the
        # bump and the standard ``max_tool_iterations`` always wins.
        max_tool_iterations_list_task: int = 10,
        review_enabled: bool = True,
        review_min_steps: int = 2,
        review_max_iterations: int = 3,
        # trajectory compression knobs. Defaults match
        # :mod:`backend.agent.context`. Set ``trajectory_compress=False``
        # to disable entirely (smoke tests, deterministic replays).
        trajectory_compress: bool = True,
        trajectory_max_chars: int = DEFAULT_MAX_CHARS,
        trajectory_keep_recent_rounds: int = DEFAULT_KEEP_RECENT_ROUNDS,
        trajectory_keep_last_tool_results: int = DEFAULT_KEEP_LAST_TOOL_RESULTS,
        trajectory_truncated_tool_chars: int = DEFAULT_TRUNCATED_TOOL_CHARS,
        # optional LLM-summary compressor injected from app.py.
        # When ``None`` the engine uses only the deterministic phases.
        summary_compressor: Optional["SummaryCompressor"] = None,
        summary_threshold_chars: Optional[int] = None,
        # answer cache injected from app.py. ``None`` disables
        # the cache entirely (smoke tests that don't need it, or a
        # configuration where SQLite isn't available). When set, every
        # IM turn checks the wiki BEFORE calling the LLM, and writes
        # back asynchronously after a successful no-tool turn.
        wiki_store: Optional["WikiStore"] = None,
        # Token-level streaming → IM dispatch. ``True`` forwards partial
        # assistant text to the gateway as the LLM streams; ``False``
        # batches a single send at end-of-turn. The flush thresholds bound
        # the cadence so we don't either flood IM rate-limits (too small)
        # or defeat the point of streaming (too large). All are honoured
        # only when the gateway exposes a streaming dispatch fn AND the
        # routed skill does NOT carry ``rich_output: true`` (rich-content
        # fence streaming would leak half-rendered JSON to users).
        stream_to_im_enabled: bool = True,
        stream_flush_chars: int = 200,
        stream_flush_interval_ms: int = 1500,
        stream_min_chars: int = 80,
        # atomic-fact crystallizer (post-answer fact
        # extraction).  ``None`` disables the pipeline entirely.
        # When set AND the routed skill manifest carries
        # ``crystallize=True``, every successful no-tool turn fires
        # a fire-and-forget second LLM call distilling 3-5 atomic
        # facts and writing them to the wiki for future short-query
        # hits. See :class:`backend.wiki.crystallizer.Crystallizer`.
        crystallizer: Optional["Crystallizer"] = None,
        # Geo-first wiki lookup acceleration. When set, the wiki cache
        # lookup pre-scans the user query for known city names and tries
        # a geo-narrowed lookup BEFORE the global lookup, so
        # substring/alias scans run on a smaller subset of rows. ``None``
        # falls back to the global lookup.
        geo_store: Optional["GeoStore"] = None,
        # Flash-LLM intent router. When set, AgentLoop
        # calls the router at the start of every interactive turn and
        # injects the recommended knowledge_mode / skill into the
        # Pro model's system prompt. Fail-soft: a router timeout or
        # provider error returns an empty decision and the Pro model
        # runs against the unchanged prompt. ``None`` disables the
        # router entirely (default).
        # ``workspace_dir`` is needed to enumerate available
        # knowledge_modes when building the router catalog; if left
        # ``None`` the router is fed an empty mode catalog and will
        # only consider skills.
        router_llm: Optional["RouterLLM"] = None,
        workspace_dir: Optional["Path"] = None,
        # v1.2.0 — bounded concurrency for parallel-capable tool calls
        # in a single agent step. Passed through to ToolLoopRunner.
        # Defaults to 4; clamped to >=1 by the runner.
        tool_loop_parallel_max_concurrency: int = 4,
        permission_policy: Optional["PermissionPolicy"] = None,
        tool_execution: Optional[HarnessExecution] = None,
        tracer: Optional["TraceRecorder"] = None,
    ) -> None:
        self._llm = llm
        self._registry = tool_registry
        self._tool_execution = tool_execution or (
            HarnessExecution(tool_registry) if tool_registry is not None else None
        )
        self._tracer = tracer
        self._store = confirmation_store
        self._skill_loader = skill_loader
        self._memory = memory_manager
        self._failure_learner = failure_learner
        self._wiki_store = wiki_store
        self._crystallizer = crystallizer
        # geo-first wiki lookup; ``None`` falls back to
        # legacy global lookup behaviour.
        self._geo_store = geo_store
        # Flash-LLM router. ``None`` disables routing entirely so the
        # Pro model sees the unchanged prompt. ``workspace_dir`` is used
        # by the catalog builder to enumerate available knowledge_modes;
        # if absent we only feed the router the skill catalog.
        self._router_llm = router_llm
        self._routing_policy = TurnRoutingPolicy(
            skill_loader=skill_loader,
            memory_manager=memory_manager,
            router_llm=router_llm,
            workspace_dir=workspace_dir,
        )
        self._retrieval_policy = WikiRetrievalPolicy(
            wiki_store=wiki_store,
            geo_store=geo_store,
        )
        self._stream_to_im_enabled = bool(stream_to_im_enabled)
        self._stream_flush_chars = max(1, int(stream_flush_chars))
        self._stream_flush_interval_ms = max(0, int(stream_flush_interval_ms))
        self._stream_min_chars = max(0, int(stream_min_chars))
        # per-turn loop guardrail controller. ``None`` disables
        # the guardrail entirely (used by smoke tests that exercise the
        # loop without the controller wired). Production callers in
        # ``app.py`` always pass a controller.
        self._tool_guardrails = tool_guardrails
        self._max_tool_iterations = max_tool_iterations
        self._max_tool_iterations_list_task = max_tool_iterations_list_task
        self._review_enabled = review_enabled
        self._review_min_steps = review_min_steps
        self._review_max_iterations = review_max_iterations
        self._trajectory_compress = trajectory_compress
        self._context_engine = ContextEngine(
            max_chars=trajectory_max_chars,
            keep_recent_rounds=trajectory_keep_recent_rounds,
            keep_last_tool_results=trajectory_keep_last_tool_results,
            truncated_tool_chars=trajectory_truncated_tool_chars,
            summary_compressor=summary_compressor,
            summary_threshold_chars=summary_threshold_chars,
        )
        self._tool_loop = ToolLoopRunner(
            registry=tool_registry,
            guardrails=tool_guardrails,
            context_engine=self._context_engine,
            max_iterations=max_tool_iterations,
            trajectory_compress=trajectory_compress,
            suspend_fn=self._suspend_for_confirmation,
            parallel_max_concurrency=tool_loop_parallel_max_concurrency,
            permission_policy=permission_policy,
            execution=self._tool_execution,
        )
        self._post_turn = PostTurnPipeline(
            memory=memory_manager,
            failure_learner=failure_learner,
            wiki_store=wiki_store,
            crystallizer=crystallizer,
            tool_loop_runner=self._tool_loop,
            registry=tool_registry,
            review_enabled=review_enabled,
            review_min_steps=review_min_steps,
            review_max_iterations=review_max_iterations,
        )
        self._confirmation_flow = ConfirmationFlow(
            store=confirmation_store,
            execution=self._tool_execution,
            memory_sync_fn=self._sync_memory_turn,
            run_tool_loop_fn=self._run_tool_loop,
            system_prompt_dm=SYSTEM_PROMPT_DM,
        )
        self._cron_runner = CronRunner(
            memory=memory_manager,
            failure_learner=failure_learner,
            skill_loader=skill_loader,
            post_turn=self._post_turn,
            run_tool_loop_fn=self._run_tool_loop,
            sync_memory_fn=self._sync_memory_turn,
        )
        self._turn_preparer = TurnPreparer(
            store=confirmation_store,
            memory=memory_manager,
            registry=tool_registry,
            skill_loader=skill_loader,
            routing_policy=self._routing_policy,
            retrieval_policy=self._retrieval_policy,
            router_llm=router_llm,
            resume_fn=self._resume,
            prefetch_travel_fn=self._prefetch_travel_realtime,
            sync_memory_fn=self._sync_memory_turn,
        )

    @property
    def llm_configured(self) -> bool:
        return bool(self._llm and self._llm.configured)

    @property
    def tool_count(self) -> int:
        return len(self._registry) if self._registry else 0

    # =================================================================
    # Public entry points
    # =================================================================

    async def run_turn(
        self,
        message: IncomingMessage,
        *,
        dispatch_fn: Optional[Callable] = None,
    ) -> Optional[OutgoingMessage]:
        started = time.perf_counter()
        wall_started = time.time()
        ok = True
        error: Optional[str] = None
        try:
            return await self._run_turn_impl(message, dispatch_fn=dispatch_fn)
        except Exception as exc:
            ok = False
            error = redact_text(f"{type(exc).__name__}: {exc}")
            raise
        finally:
            if self._tracer is not None:
                self._tracer.record_turn(
                    TurnRecord(
                        started_at=wall_started,
                        elapsed_ms=int((time.perf_counter() - started) * 1000),
                        platform=message.platform,
                        user_id=message.user_id or "",
                        text_preview=redact_text(message.text or "")[:80],
                        ok=ok,
                        error=error,
                    )
                )

    async def _run_turn_impl(
        self,
        message: IncomingMessage,
        *,
        dispatch_fn: Optional[Callable] = None,
    ) -> Optional[OutgoingMessage]:
        turn_started = time.perf_counter()
        # v0.40.8 — log attachment counts so image-only inbound messages
        # are visible in the perf log; previously the operator had to
        # spelunk the gateway raw payload to know an image even arrived.
        image_count = sum(
            1 for att in message.attachments
            if att.kind == "image" and att.url
        )
        logger.info(
            "agent turn: platform={} user={} text={!r} attachments={} (image={})",
            message.platform, message.user_id, (message.text or "")[:200],
            len(message.attachments), image_count,
        )
        if message.reply_target is None:
            return None
        text = message.text or ""
        session_id = f"{message.platform}:{message.user_id or ''}"
        turn_id = f"{message.platform}:{message.user_id or ''}:{int(turn_started * 1000)}"
        ctx = TurnContext(
            turn_id=turn_id,
            session_id=session_id,
            platform=message.platform,
            user_id=message.user_id or "",
            reply_target=message.reply_target,
            is_interactive=True,
            user_message=text,
        )
        with set_turn_context(ctx):
            # v0.40.8 — image-only messages must reach TurnPreparer so the
            # multimodal user LLMMessage gets built. Pre-v0.40.8 dropped
            # them here because ``text == ""`` and the agent had nothing
            # to feed the LLM. Now we synthesize a short prompt placeholder
            # below in TurnPreparer when text is empty but images attached;
            # only truly empty turns (no text + no image) still short-circuit.
            if not text and image_count == 0:
                return None
            turn = await self._prepare_turn(message, text, session_id, dispatch_fn, turn_started)
            if turn.early_reply is not None or not turn.history:
                return turn.early_reply
            return await self._execute_prepared_turn(turn, message, dispatch_fn, turn_started)

    async def _prepare_turn(
        self,
        message: IncomingMessage,
        text: str,
        session_id: str,
        dispatch_fn,
        turn_started: float,
    ):
        return await self._turn_preparer.prepare(
            message, text, session_id, dispatch_fn, turn_started,
            llm_configured=self.llm_configured,
        )

    async def _execute_prepared_turn(
        self,
        turn,
        message: IncomingMessage,
        dispatch_fn,
        turn_started: float,
    ):
        if turn.streaming_skill and turn.routed_manifest is not None:
            return await self._execute_streaming_prepared_turn(
                turn=turn,
                message=message,
                dispatch_fn=dispatch_fn,
                turn_started=turn_started,
            )
        return await self._execute_standard_prepared_turn(
            turn=turn,
            message=message,
            turn_started=turn_started,
        )

    async def _execute_streaming_prepared_turn(
        self,
        *,
        turn,
        message: IncomingMessage,
        dispatch_fn,
        turn_started: float,
    ):
        invoked_skill_id = turn.routed_skill_id
        invoked_skill_manifest = turn.routed_manifest
        _base_history: list[LLMMessage] = list(turn.history[:-1])
        _full_answer = await self._run_streaming_skill(
            manifest=invoked_skill_manifest,
            safe_text=turn.safe_text,
            session_id=turn.session_id,
            reply_target=message.reply_target,
            dispatch_fn=dispatch_fn,
            base_history=_base_history,
        )
        logger.info(
            "[perf] agent.run_turn STREAMING platform={} user={} sections={} elapsed_ms={}",
            message.platform,
            message.user_id,
            len(invoked_skill_manifest.streaming_sections),
            int((time.perf_counter() - turn_started) * 1000),
        )
        self._sync_memory_turn(
            user_content=turn.safe_text,
            assistant_content=_full_answer,
            session_id=turn.session_id,
            metadata={
                "platform": message.platform,
                "user_id": message.user_id or "",
                "interactive": True,
                "tool_outcomes": (),
                "invoked_tools": (),
                "skill_hint": invoked_skill_id,
                "streaming": True,
            },
            outcome=None,
        )
        if self._wiki_store is not None and invoked_skill_manifest.wiki_cache_enabled and _full_answer:
            self._post_turn._schedule_wiki_write(
                skill_id=invoked_skill_id,
                raw_query=turn.safe_text,
                answer=_full_answer,
                ttl_seconds=invoked_skill_manifest.wiki_cache_ttl_seconds,
                metadata={
                    "platform": message.platform,
                    "skill_version": invoked_skill_manifest.version,
                    "model": getattr(self._llm, "model", "") if self._llm else "",
                    "streaming": True,
                },
            )
        if self._crystallizer is not None and invoked_skill_manifest.crystallize and _full_answer:
            self._post_turn._schedule_crystallize(
                skill_id=invoked_skill_id,
                answer=_full_answer,
                ttl_seconds=invoked_skill_manifest.wiki_cache_ttl_seconds,
            )
        if dispatch_fn is not None:
            return None
        return OutgoingMessage(
            target=message.reply_target or DeliveryTarget(
                platform=message.platform,
                target_type="user",
                target_id=message.user_id or "",
            ),
            text=_full_answer or "这一回没产生回复，可以换个问法再试一下。",
        )

    async def _execute_standard_prepared_turn(
        self,
        *,
        turn,
        message: IncomingMessage,
        turn_started: float,
    ):
        invoked_skill_id = turn.routed_skill_id
        invoked_skill_manifest = turn.routed_manifest
        try:
            adaptive_iter = _adaptive_tool_budget(
                turn.safe_text,
                base=self._max_tool_iterations,
                list_task=self._max_tool_iterations_list_task,
            )
            outcome = await self._run_tool_loop(
                turn.history,
                system=turn.system_prompt,
                interactive=True,
                platform=message.platform,
                user_id=message.user_id or "",
                reply_target=message.reply_target,
                user_text_for_fallback=turn.safe_text,
                max_iterations=adaptive_iter,
                session_id=turn.session_id,
            )
        finally:
            logger.info(
                "[perf] agent.run_turn platform={} user={} elapsed_ms={}",
                message.platform,
                message.user_id,
                int((time.perf_counter() - turn_started) * 1000),
            )
        self._post_turn_feedback(
            outcome=outcome,
            safe_text=turn.safe_text,
            session_id=turn.session_id,
            platform=message.platform,
            user_id=message.user_id or "",
            invoked_skill_id=invoked_skill_id,
            invoked_skill_manifest=invoked_skill_manifest,
            history=turn.history,
            memory_intent=turn.memory_intent,
        )
        return outcome.to_message(reply_target=message.reply_target)


    async def _prefetch_travel_realtime(self, query: str) -> str:
        """Run ``travel_realtime(action='bundle')`` directly so the user
        always sees real 12306 / weather data when a route or realtime
        keyword is detected. Returns the formatted text on success, or an
        empty string when the tool is unavailable / failed / returned no
        usable content. Never raises — failures fall through to the
        normal LLM path.
        """
        if self._tool_execution is None:
            return ""
        if self._tool_execution.registry.get("travel_realtime") is None:
            return ""
        try:
            result = await self._tool_execution.execute(
                "travel_realtime",
                {"action": "bundle", "query": query},
                session_id="travel:prefetch",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[skill] travel_realtime prefetch raised: {}", exc)
            return ""
        if not getattr(result, "ok", False):
            return ""
        content = (getattr(result, "content", "") or "").strip()
        if not content:
            return ""
        if not ("12306" in content or "天气" in content or "🚄" in content or "🌤" in content):
            return ""
        return content

    async def _run_streaming_skill(
        self,
        *,
        manifest: "SkillManifest",
        safe_text: str,
        session_id: str,
        reply_target: Optional[DeliveryTarget],
        dispatch_fn: Optional[Callable],
        base_history: list[LLMMessage],
    ) -> str:
        return await run_streaming_skill(
            llm=self._llm,
            manifest=manifest,
            safe_text=safe_text,
            session_id=session_id,
            reply_target=reply_target,
            dispatch_fn=dispatch_fn,
            base_history=base_history,
        )

    async def generate_cron_message(
        self,
        instruction: str,
        *,
        job_name: str,
        skill_hint: Optional[str] = None,
        pre_script_output: Optional[str] = None,
    ) -> str:
        return await self._cron_runner.generate(
            instruction,
            job_name=job_name,
            skill_hint=skill_hint,
            pre_script_output=pre_script_output,
            llm_configured=self.llm_configured,
            llm=self._llm,
        )

    @staticmethod
    def _compose_cron_user_message(instruction: str, pre_script_output: Optional[str]) -> str:
        return compose_cron_user_message(instruction, pre_script_output)

    def _build_cron_system_prompt(self, skill_hint: Optional[str]) -> str:
        return build_cron_system_prompt(skill_hint, self._skill_loader)


    # =================================================================
    # Resume path
    # =================================================================

    async def _resume(
        self,
        pending,
        decision: bool,
        message,
    ):
        return await self._confirmation_flow.resume(
            pending, decision, message, llm_configured=self.llm_configured,
        )

    async def _suspend_for_confirmation(
        self,
        *,
        tc,
        history,
        platform: str,
        user_id: str,
        reply_target,
        system: str,
    ):
        return await self._confirmation_flow.suspend(
            tc=tc, history=history,
            platform=platform, user_id=user_id,
            reply_target=reply_target, system=system,
        )

    # =================================================================
    # Core loop (delegated to ToolLoopRunner)
    # =================================================================

    async def _run_tool_loop(
        self,
        history: list[LLMMessage],
        *,
        system: str,
        interactive: bool,
        platform: str,
        user_id: str,
        reply_target: Optional[DeliveryTarget],
        user_text_for_fallback: str,
        tool_whitelist: Optional[set[str]] = None,
        trust_confirm_tools: bool = False,
        max_iterations: Optional[int] = None,
        action_filter: Optional[Callable[[str, dict], Optional[str]]] = None,
        session_id: Optional[str] = None,
    ) -> LoopOutcome:
        return await self._tool_loop.run(
            history,
            llm=self._llm,
            system=system,
            interactive=interactive,
            platform=platform,
            user_id=user_id,
            reply_target=reply_target,
            user_text_for_fallback=user_text_for_fallback,
            tool_whitelist=tool_whitelist,
            trust_confirm_tools=trust_confirm_tools,
            max_iterations=max_iterations,
            action_filter=action_filter,
            session_id=session_id,
        )

    # =================================================================
    # Post-turn (delegated to PostTurnPipeline)
    # =================================================================

    def _sync_memory_turn(
        self,
        *,
        user_content: str,
        assistant_content: str,
        session_id: str,
        metadata: dict,
        outcome: Optional[LoopOutcome],
    ) -> None:
        self._post_turn.sync_memory_turn(
            user_content=user_content,
            assistant_content=assistant_content,
            session_id=session_id,
            metadata=metadata,
            outcome=outcome,
        )

    def _post_turn_feedback(
        self,
        *,
        outcome: "LoopOutcome",
        safe_text: str,
        session_id: str,
        platform: str,
        user_id: str,
        invoked_skill_id: Optional[str],
        invoked_skill_manifest: Optional[object],
        history: "list[LLMMessage]",
        memory_intent: Optional["MemoryIntentSignal"],
    ) -> None:
        self._post_turn._crystallizer = self._crystallizer
        self._post_turn._wiki_store = self._wiki_store
        self._post_turn.handle(
            outcome=outcome,
            safe_text=safe_text,
            session_id=session_id,
            platform=platform,
            user_id=user_id,
            invoked_skill_id=invoked_skill_id,
            invoked_skill_manifest=invoked_skill_manifest,
            history=history,
            memory_intent=memory_intent,
            llm=self._llm,
        )

    @staticmethod
    def _review_action_filter(tool_name: str, arguments: dict) -> Optional[str]:
        return PostTurnPipeline.review_action_filter(tool_name, arguments)

    @staticmethod
    def _summarize_main_turn(history: list[LLMMessage]) -> str:
        return PostTurnPipeline.summarize_main_turn(history)

    async def run_end_of_day_review(
        self, user_block: str, *, max_iterations: int = 8,
    ) -> dict:
        """Public entry point for the v0.43 Phase B daily review.

        Delegates to :meth:`PostTurnPipeline.run_end_of_day_review`. The
        ``user_block`` is built by :class:`DailyReviewService` (24 h
        memory writes + skill usage stats) — we just hand it to the
        review fork with the looser end-of-day action filter. Always
        returns a dict; failures encode themselves as ``ok=False``
        rather than raising, matching the rest of the review pipeline's
        "never break the host" stance.
        """
        return await self._post_turn.run_end_of_day_review(
            llm=self._llm,
            user_block=user_block,
            max_iterations=max_iterations,
        )

    def _should_trigger_review(
        self,
        outcome: LoopOutcome,
        *,
        correction: Optional[CorrectionSignal] = None,
        memory_intent: Optional[MemoryIntentSignal] = None,
    ) -> bool:
        if not self._review_enabled:
            return False
        return self._post_turn._should_trigger_review(
            outcome, llm=self._llm, correction=correction, memory_intent=memory_intent,
        )

    def _schedule_crystallize(self, *, skill_id: str, answer: str, ttl_seconds: Optional[int]) -> None:
        self._post_turn._crystallizer = self._crystallizer
        self._post_turn._schedule_crystallize(skill_id=skill_id, answer=answer, ttl_seconds=ttl_seconds)

    def _schedule_wiki_write(self, *, skill_id: str, raw_query: str, answer: str, ttl_seconds: Optional[int], metadata: dict) -> None:
        self._post_turn._wiki_store = self._wiki_store
        self._post_turn._schedule_wiki_write(skill_id=skill_id, raw_query=raw_query, answer=answer, ttl_seconds=ttl_seconds, metadata=metadata)

    @property
    def _pending_crystallize(self):
        return self._post_turn._pending_crystallize

    @property
    def _pending_wiki_writes(self):
        return self._post_turn._pending_wiki_writes

    @property
    def _pending_reviews(self):
        return self._post_turn._pending_reviews

    # =================================================================
    # Helpers
    # =================================================================

    # confirmation formatters live under ``agent.confirmation``.
    # The shims below preserve the public ``AgentLoop._format_…``
    # surface used by smoke tests and (legacy) tooling.
    _format_confirmation_question = staticmethod(_lc.format_confirmation_question)
    _format_skill_confirmation = staticmethod(_lc.format_skill_confirmation)
    _format_cron_confirmation = staticmethod(_lc.format_cron_confirmation)
    _format_send_message_confirmation = staticmethod(_lc.format_send_message_confirmation)
    _cron_action_label = staticmethod(_lc.cron_action_label)
    _direct_confirmation_reply = staticmethod(_lc.direct_confirmation_reply)
    _direct_cron_reply = staticmethod(_lc.direct_cron_reply)
    _humanize_cron = staticmethod(_lc.humanize_cron)
    _humanize_day_part = staticmethod(_lc.humanize_day_part)
    _confirmation_preview = staticmethod(_lc.confirmation_preview)
