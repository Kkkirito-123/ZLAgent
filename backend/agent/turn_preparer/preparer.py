"""Prepares a turn from an IncomingMessage into a PreparedTurn.

Single responsibility: take a raw user message and produce a fully
assembled PreparedTurn ready for execution. Handles:

  1. Resume check (open confirmation)
  2. Command / no-LLM early exits
  3. Memory + router prompt prep
  4. Skill routing
  5. Travel realtime prefetch fast path
  6. Wiki cache fast path
  7. Stable prompt + skill body splicing
  8. Conversation history assembly

Early exits populate ``PreparedTurn.early_reply`` with the result;
the caller short-circuits when it sees an early_reply.
"""
from __future__ import annotations

import time
from typing import Any, Optional, TYPE_CHECKING

from loguru import logger

from ...gateways.base import DeliveryTarget, IncomingMessage, OutgoingMessage
from ...llm import LLMMessage
from ...llm.prompt_cache import assemble_system_prompt
from ...memory.intent import detect_memory_intent
from ...memory.manager import sanitize_untrusted
from ..confirmation import classify_decision
from ..prompts import (
    SKILL_PROMPT_HEADER,
    SYSTEM_PROMPT_DM,
    _current_time_prompt_block,
    _router_hint_block,
)
from ..context import MemorySnapshot, RuntimeSnapshot, TurnContext
from .types import PreparedTurn

if TYPE_CHECKING:
    from ...db.confirmations import ConfirmationStore
    from ...memory.manager import MemoryManager
    from ...skills.loader import SkillLoader
    from ...tools import ToolRegistry
    from ..retrieval import WikiRetrievalPolicy
    from ..routing import TurnRoutingPolicy
    from ..routing.llm_router import RouterLLM


class TurnPreparer:
    def __init__(
        self,
        *,
        store: Optional["ConfirmationStore"] = None,
        memory: Optional["MemoryManager"] = None,
        registry: Optional["ToolRegistry"] = None,
        skill_loader: Optional["SkillLoader"] = None,
        routing_policy: "TurnRoutingPolicy",
        retrieval_policy: "WikiRetrievalPolicy",
        router_llm: Optional["RouterLLM"] = None,
        resume_fn,
        prefetch_travel_fn,
        sync_memory_fn,
    ) -> None:
        self._store = store
        self._memory = memory
        self._registry = registry
        self._skill_loader = skill_loader
        self._routing_policy = routing_policy
        self._retrieval_policy = retrieval_policy
        self._router_llm = router_llm
        self._resume_fn = resume_fn
        self._prefetch_travel_fn = prefetch_travel_fn
        self._sync_memory_fn = sync_memory_fn

    async def prepare(
        self,
        message: IncomingMessage,
        text: str,
        session_id: str,
        dispatch_fn,
        turn_started: float,
        *,
        llm_configured: bool,
    ) -> PreparedTurn:
        # Phase 1: Resume + command + LLM gate
        early = await self._check_early_exit(message, text, session_id, llm_configured)
        if early is not None:
            return early

        # Phase 2: Memory + router dynamic suffix
        # v0.40.8 — image-only turns reach this path with ``text == ""``;
        # we still need a non-empty prompt for the LLM, otherwise the
        # multimodal payload becomes ``[{"type":"image_url",...}]`` with
        # nothing telling the model what the user wants. Synthesize a
        # short Chinese placeholder so vision models get a clear "请描述
        # 这张图" cue. This runs before sanitize_untrusted so the
        # placeholder still passes through the same safety pipe as user
        # input.
        if not text and any(
            att.kind == "image" and att.url for att in message.attachments
        ):
            text = "请描述/分析用户刚发的图片。如果图里有可识别的物体、文字、场景或问题，按用户可能的意图回答。"
        safe_text = sanitize_untrusted(text)
        memory_intent = detect_memory_intent(safe_text)
        dynamic_suffix, memory_snapshot, runtime_snapshot = await self._build_dynamic_suffix(safe_text, message, session_id)

        # Phase 3: Skill route
        skill_route = self._routing_policy.pick_skill_route(safe_text, session_id)
        routed_skill_id = skill_route.skill_id
        routed_manifest = skill_route.manifest

        # Phase 4: Travel realtime fast path
        travel = await self._try_travel_prefetch(
            safe_text, message, session_id, dispatch_fn,
            routed_skill_id, routed_manifest,
            dynamic_suffix, memory_intent,
        )
        if travel is not None:
            return travel

        # Phase 5: Wiki cache fast path
        wiki = self._try_wiki_fast_path(
            safe_text, message, session_id, dispatch_fn,
            routed_skill_id, routed_manifest,
            dynamic_suffix, memory_intent, turn_started,
        )
        if wiki is not None:
            return wiki

        # Phase 6: Skill body splicing + final system prompt
        stable_prompt, invoked_id, invoked_manifest = self._build_stable_prompt(
            routed_skill_id, routed_manifest,
        )
        system_prompt = assemble_system_prompt(stable_prompt, dynamic_suffix)

        # Phase 7: History assembly
        # v0.40.8: pull image URLs out of inbound attachments so the
        # user LLMMessage carries them through to the multimodal
        # serializer in ``LLMMessage.to_api()``. Non-image / URL-less
        # attachments are dropped here — the LLM only consumes images.
        image_urls = [
            att.url for att in message.attachments
            if att.kind == "image" and att.url
        ]
        history = self._build_history(
            safe_text, system_prompt, session_id, image_urls=image_urls,
        )

        streaming = bool(
            invoked_id and invoked_manifest
            and invoked_manifest.streaming_sections
            and llm_configured
        )
        return PreparedTurn(
            safe_text=safe_text, session_id=session_id,
            platform=message.platform, user_id=message.user_id or "",
            reply_target=message.reply_target,
            stable_prompt=stable_prompt, dynamic_suffix=dynamic_suffix,
            history=history, system_prompt=system_prompt,
            routed_skill_id=invoked_id, routed_manifest=invoked_manifest,
            memory_intent=memory_intent, streaming_skill=streaming,
            dispatch_fn=dispatch_fn,
        )

    # ---------- Phase 1 ----------

    async def _check_early_exit(
        self, message: IncomingMessage, text: str, session_id: str, llm_configured: bool,
    ) -> Optional[PreparedTurn]:
        def _early(reply: Optional[OutgoingMessage]) -> PreparedTurn:
            return PreparedTurn(
                safe_text=text, session_id=session_id,
                platform=message.platform, user_id=message.user_id or "",
                reply_target=message.reply_target,
                stable_prompt=SYSTEM_PROMPT_DM, dynamic_suffix="", history=[],
                early_reply=reply,
            )

        # Resume open confirmation
        if self._store is not None and message.user_id and text:
            pending = self._store.find_pending_for(
                platform=message.platform, user_id=message.user_id,
            )
            if pending is not None:
                decision = classify_decision(text)
                if decision is not None:
                    return _early(await self._resume_fn(pending, decision, message))
                logger.info(
                    "user {} has pending confirmation #{} but reply {!r} is"
                    " not yes/no \u2014 treating as new turn",
                    message.user_id, pending.id, text[:60],
                )

        # /new + /reset
        command_text = text.strip().lower()
        if (
            command_text in ("/new", "/reset")
            or command_text.startswith(("/new ", "/reset "))
        ):
            details: list[str] = []
            if self._memory is not None:
                details = self._memory.reset_session(
                    session_id=session_id, reason=command_text,
                    metadata={
                        "platform": message.platform,
                        "user_id": message.user_id or "",
                        "message_id": message.message_id,
                    },
                )
            suffix = ("\n" + "\n".join(details)) if details else ""
            return _early(OutgoingMessage(
                target=message.reply_target,
                text=f"\u5df2\u4fdd\u5b58\u5e76\u6e05\u7a7a\u672c\u4f1a\u8bdd\u4e0a\u4e0b\u6587\u3002{suffix}",
            ))

        # No-LLM echo. v0.45 — friendlier text; the operator sees this
        # only when the LLM provider isn't configured at all, so we
        # explain instead of dumping a debug-style prefix.
        if not llm_configured:
            return _early(OutgoingMessage(
                target=message.reply_target,
                text="后端模型还没接通，先把消息收下了。配好 LLM 再聊。",
            ))

        return None

    # ---------- Phase 2 ----------

    async def _build_dynamic_suffix(
        self, safe_text: str, message: IncomingMessage, session_id: str,
    ) -> tuple[str, Optional[MemorySnapshot], Optional[RuntimeSnapshot]]:
        sections: list[str] = [_current_time_prompt_block()]
        memory_snapshot: Optional[MemorySnapshot] = None
        runtime_snapshot = RuntimeSnapshot(
            llm_configured=bool(getattr(self._router_llm, "configured", False)),
            tool_count=len(self._registry) if self._registry is not None else 0,
            core_status={"interactive": True},
            service_status={},
            extension_status={},
        )
        if self._memory is not None:
            self._memory.on_turn_start(
                safe_text, platform=message.platform,
                user_id=message.user_id or "", interactive=True,
                session_id=session_id,
            )
            memory_snapshot = MemorySnapshot(
                control_axioms=self._memory._store.list(kind="control_axiom") if hasattr(self._memory, "_store") else [],
                agent_notes=self._memory._store.list(kind="agent_note") if hasattr(self._memory, "_store") else [],
                user_facts=self._memory._store.list(kind="user_fact") if hasattr(self._memory, "_store") else [],
                provider_blocks=self._memory._provider_system_blocks() if hasattr(self._memory, "_provider_system_blocks") else [],
                rendered_block=self._memory.system_prompt_block(),
            )
            if memory_snapshot.rendered_block:
                sections.append(memory_snapshot.rendered_block)
        if self._router_llm is not None:
            try:
                router_decision = await self._routing_policy.route_intent(safe_text)
                hint = _router_hint_block(router_decision)
                if hint:
                    sections.append(hint)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[router] route_intent unexpectedly raised: {}: {}",
                    type(exc).__name__, exc,
                )
        return "\n\n".join(s for s in sections if s), memory_snapshot, runtime_snapshot

    # ---------- Phase 4 ----------

    async def _try_travel_prefetch(
        self,
        safe_text: str,
        message: IncomingMessage,
        session_id: str,
        dispatch_fn,
        routed_skill_id: Optional[str],
        routed_manifest,
        dynamic_suffix: str,
        memory_intent,
    ) -> Optional[PreparedTurn]:
        if not self._routing_policy.should_realtime_prefetch(
            safe_text, routed_skill_id,
            has_travel_realtime_tool=(
                self._registry is not None
                and self._registry.get("travel_realtime") is not None
            ),
        ):
            return None

        hint_text = self._routing_policy.realtime_loading_hint(safe_text)
        if hint_text:
            # v0.45 — route through the progress emitter's per-turn
            # budget so this travel-specific loading hint participates
            # in the same ack-coordination that suppresses thinking-ack
            # and tool-ping spam. ``try_emit_inline_via_ctx`` returns
            # False when no per-turn sink is bound (e.g. cron-driven
            # turns); in that fallback we dispatch directly so legacy
            # paths that never had a sink still show the hint.
            from ...harness.progress.emitter import try_emit_inline_via_ctx
            sent = await try_emit_inline_via_ctx(hint_text)
            if not sent and dispatch_fn is not None:
                try:
                    await dispatch_fn(OutgoingMessage(target=message.reply_target, text=hint_text))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[skill] travel_realtime hint dispatch failed: {}", exc)

        prefetch_text = await self._prefetch_travel_fn(safe_text)
        if not prefetch_text:
            return None

        logger.info(
            "[skill] travel_realtime prefetch dispatched chars={} platform={} user={}",
            len(prefetch_text), message.platform, message.user_id,
        )
        if dispatch_fn is not None:
            try:
                await dispatch_fn(OutgoingMessage(target=message.reply_target, text=prefetch_text))
            except Exception as exc:  # noqa: BLE001
                logger.warning("[skill] travel_realtime dispatch failed: {}", exc)
        self._sync_memory_fn(
            user_content=safe_text, assistant_content=prefetch_text,
            session_id=session_id,
            metadata={
                "platform": message.platform, "user_id": message.user_id or "",
                "interactive": True,
                "tool_outcomes": (("travel_realtime", True),),
                "invoked_tools": ("travel_realtime",),
                "skill_hint": "travel-realtime-mcp", "realtime_prefetch": True,
            },
            outcome=None,
        )
        travel_reply = None if dispatch_fn is not None else OutgoingMessage(
            target=message.reply_target or DeliveryTarget(
                platform=message.platform, target_type="user",
                target_id=message.user_id or "",
            ),
            text=prefetch_text,
        )
        return PreparedTurn(
            safe_text=safe_text, session_id=session_id,
            platform=message.platform, user_id=message.user_id or "",
            reply_target=message.reply_target,
            stable_prompt=SYSTEM_PROMPT_DM, dynamic_suffix=dynamic_suffix, history=[],
            routed_skill_id=routed_skill_id, routed_manifest=routed_manifest,
            memory_intent=memory_intent, early_reply=travel_reply,
        )

    # ---------- Phase 5 ----------

    def _try_wiki_fast_path(
        self,
        safe_text: str,
        message: IncomingMessage,
        session_id: str,
        dispatch_fn,
        routed_skill_id: Optional[str],
        routed_manifest,
        dynamic_suffix: str,
        memory_intent,
        turn_started: float,
    ) -> Optional[PreparedTurn]:
        if not self._retrieval_policy.should_lookup_fast_path(
            skill_id=routed_skill_id, manifest=routed_manifest,
            streaming_to_im=dispatch_fn is not None,
        ):
            return None
        try:
            hit = self._retrieval_policy.lookup_fast_path(
                skill_id=routed_skill_id, manifest=routed_manifest,
                query=safe_text, streaming_to_im=dispatch_fn is not None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "wiki lookup failed skill={} err={}; falling through to LLM",
                routed_skill_id, exc,
            )
            return None
        if hit is None:
            return None

        logger.info(
            "[perf] agent.run_turn FAST_PATH platform={} user={} skill={} kind={} elapsed_ms={}",
            message.platform, message.user_id, routed_skill_id,
            hit.similarity_kind,
            int((time.perf_counter() - turn_started) * 1000),
        )
        self._sync_memory_fn(
            user_content=safe_text, assistant_content=hit.answer,
            session_id=session_id,
            metadata={
                "platform": message.platform, "user_id": message.user_id or "",
                "interactive": True, "tool_outcomes": (), "invoked_tools": (),
                "skill_hint": routed_skill_id, "wiki_hit": True,
                "wiki_similarity_kind": hit.similarity_kind,
                "wiki_hits_total": hit.hit_count,
            },
            outcome=None,
        )
        return PreparedTurn(
            safe_text=safe_text, session_id=session_id,
            platform=message.platform, user_id=message.user_id or "",
            reply_target=message.reply_target,
            stable_prompt=SYSTEM_PROMPT_DM, dynamic_suffix=dynamic_suffix, history=[],
            routed_skill_id=routed_skill_id, routed_manifest=routed_manifest,
            memory_intent=memory_intent,
            early_reply=OutgoingMessage(
                target=message.reply_target or DeliveryTarget(
                    platform=message.platform, target_type="user",
                    target_id=message.user_id or "",
                ),
                text=hit.answer,
            ),
        )

    # ---------- Phase 6 ----------

    def _build_stable_prompt(
        self, routed_skill_id: Optional[str], routed_manifest,
    ) -> tuple[str, Optional[str], Any]:
        stable_prompt = SYSTEM_PROMPT_DM
        invoked_id: Optional[str] = None
        invoked_manifest = None
        if routed_skill_id is not None and self._skill_loader is not None:
            body = self._skill_loader.read_body(routed_skill_id)
            if body:
                skill_label = routed_manifest.name if routed_manifest else routed_skill_id
                stable_prompt = (
                    f"{SYSTEM_PROMPT_DM}\n\n"
                    f"--- {SKILL_PROMPT_HEADER} ---\n"
                    f"# \u6280\u80fd: {skill_label}\n"
                    f"{body}\n"
                    f"--- \u6280\u80fd\u6b63\u6587\u7ed3\u675f ---"
                )
                invoked_id = routed_skill_id
                invoked_manifest = routed_manifest
        return stable_prompt, invoked_id, invoked_manifest

    # ---------- Phase 7 ----------

    def _build_history(
        self, safe_text: str, system_prompt: str, session_id: str,
        *, image_urls: Optional[list[str]] = None,
    ) -> list[LLMMessage]:
        history: list[LLMMessage] = [LLMMessage(role="system", content=system_prompt)]
        if self._memory is not None:
            pf = self._memory.prefetch(safe_text)
            pfb = self._memory.render_prefetch_block(pf)
            if pfb:
                history.append(LLMMessage(role="system", content=pfb))
            pvb = self._memory.provider_prefetch_block(safe_text, session_id=session_id)
            if pvb:
                history.append(LLMMessage(role="system", content=pvb))
        # ``images=None`` keeps the message identical to the pre-v0.40.8
        # text-only shape (``content`` is a string in to_api()); only
        # populated lists trigger the multimodal content-parts path.
        history.append(LLMMessage(
            role="user", content=safe_text,
            images=list(image_urls) if image_urls else None,
        ))
        return history
