from __future__ import annotations

from typing import Any, Optional, TYPE_CHECKING

from loguru import logger

from ..agent.loop import AgentLoop
from ..db.confirmations import ConfirmationStore
from ..tools.permission import PermissionPolicy
from ..agent.prompts import (
    MCP_AUTO_CONFIRM_INSTALL_ACTIONS,
    WEIXIN_AUTO_CONFIRM_SKILL_ACTIONS,
)

if TYPE_CHECKING:
    from ..core.config import Settings
    from ..llm import LLMClient
    from ..memory.manager import MemoryManager
    from ..skills.loader import SkillLoader
    from ..tools import ToolRegistry


def build_confirmation_store(settings: "Settings") -> ConfirmationStore:
    store = ConfirmationStore(default_ttl_seconds=settings.confirmation_ttl_seconds)
    logger.info("confirmation store ready (default ttl={}s)", settings.confirmation_ttl_seconds)
    return store


def build_failure_learner(settings: "Settings", memory_store: Any) -> Any:
    if not settings.failure_learning_enabled:
        return None
    from ..agent.recovery import FailureLearner
    # v0.45 — persist counters so a docker restart doesn't reset
    # everyone's failure history. Lives next to the other memory
    # sidecars under ``workspace/memory/``.
    state_path = settings.workspace_dir / "memory" / "failure_learning.json"
    return FailureLearner(
        memory_store,
        failure_threshold=settings.failure_learning_threshold,
        state_path=state_path,
    )


def build_tool_guardrails(settings: "Settings") -> Any:
    if not settings.tool_guardrails_enabled:
        return None
    from ..agent.recovery import ToolCallGuardrailConfig, ToolCallGuardrailController
    return ToolCallGuardrailController(ToolCallGuardrailConfig(
        hard_stop_enabled=settings.tool_guardrails_hard_stop_enabled,
        exact_failure_warn_after=settings.tool_guardrails_warn_after_failure,
        exact_failure_block_after=settings.tool_guardrails_block_after_failure,
        same_tool_failure_warn_after=settings.tool_guardrails_warn_after_same_tool_failure,
        same_tool_failure_halt_after=settings.tool_guardrails_halt_after_same_tool_failure,
        no_progress_warn_after=settings.tool_guardrails_warn_after_no_progress,
        no_progress_block_after=settings.tool_guardrails_block_after_no_progress,
    ))


def build_summary_compressor(
    settings: "Settings",
    llm_client: "LLMClient",
    memory_manager: "MemoryManager",
) -> Any:
    if not settings.context_summary_enabled:
        return None
    from ..agent.context import SummaryCompressor
    return SummaryCompressor(
        llm=llm_client,
        keep_recent_rounds=settings.trajectory_keep_recent_rounds,
        summary_max_chars=settings.context_summary_max_chars,
        failure_cooldown_seconds=settings.context_summary_failure_cooldown_seconds,
        memory_pre_compress=memory_manager.on_pre_compress,
    )


def build_crystallizer(
    settings: "Settings",
    llm_client: "LLMClient",
    wiki_store: Any,
    geo_store: Any,
) -> Any:
    if not llm_client.configured or wiki_store is None:
        return None
    from ..wiki.crystallizer import Crystallizer
    return Crystallizer(llm=llm_client, wiki_store=wiki_store, geo_store=geo_store)


def build_agent(
    settings: "Settings",
    *,
    llm_client: "LLMClient",
    router_llm: Any,
    tool_registry: "ToolRegistry",
    confirmation_store: ConfirmationStore,
    skill_loader: "SkillLoader",
    memory_manager: "MemoryManager",
    memory_store: Any,
    wiki_store: Any,
    geo_store: Any,
) -> AgentLoop:
    failure_learner = build_failure_learner(settings, memory_store)
    tool_guardrails = build_tool_guardrails(settings)
    summary_compressor = build_summary_compressor(settings, llm_client, memory_manager)
    crystallizer = build_crystallizer(settings, llm_client, wiki_store, geo_store)
    permission_policy = PermissionPolicy.from_iterables(
        mcp_auto_confirm_install_actions=MCP_AUTO_CONFIRM_INSTALL_ACTIONS,
        weixin_auto_confirm_skill_actions=WEIXIN_AUTO_CONFIRM_SKILL_ACTIONS,
    )

    return AgentLoop(
        llm=llm_client,
        tool_registry=tool_registry,
        confirmation_store=confirmation_store,
        skill_loader=skill_loader,
        memory_manager=memory_manager,
        failure_learner=failure_learner,
        tool_guardrails=tool_guardrails,
        max_tool_iterations=settings.agent_max_tool_iterations,
        max_tool_iterations_list_task=settings.agent_max_tool_iterations_list_task,
        review_enabled=settings.agent_review_enabled,
        review_min_steps=settings.agent_review_min_steps,
        review_max_iterations=settings.agent_review_max_iterations,
        trajectory_compress=settings.trajectory_compress_enabled,
        trajectory_max_chars=settings.trajectory_max_chars,
        trajectory_keep_recent_rounds=settings.trajectory_keep_recent_rounds,
        trajectory_keep_last_tool_results=settings.trajectory_keep_last_tool_results,
        trajectory_truncated_tool_chars=settings.trajectory_truncated_tool_chars,
        summary_compressor=summary_compressor,
        summary_threshold_chars=settings.context_summary_threshold_chars,
        wiki_store=wiki_store,
        stream_to_im_enabled=settings.agent_stream_to_im_enabled,
        stream_flush_chars=settings.agent_stream_flush_chars,
        stream_flush_interval_ms=settings.agent_stream_flush_interval_ms,
        stream_min_chars=settings.agent_stream_min_chars,
        crystallizer=crystallizer,
        geo_store=geo_store,
        router_llm=router_llm,
        workspace_dir=settings.workspace_dir,
        tool_loop_parallel_max_concurrency=settings.tool_loop_parallel_max_concurrency,
        permission_policy=permission_policy,
    )
