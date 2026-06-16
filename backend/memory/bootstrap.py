"""Memory subsystem boot helper.

Extracted in v0.40 from :func:`backend.app.lifespan`. Builds the
v0.12 store + manager and (optionally) wires the v0.37.2
SessionContextProvider so a same-IM-session continuation can inherit
slot-fill defaults (destination, budget, date) without the user
having to repeat them.

v0.43 also seeds the **L1 control-theory axioms** (钱学森工程控制论
核心思想) on first boot — these are the principles the agent thinks
WITH, not facts it learned. They are pinned so the LRU sweep won't
touch them, sourced as ``import`` so provenance reflects "system-
seeded" rather than "user-said", and written via the same store as
every other memory so the system_prompt_block surfaces them
uniformly. The seed is idempotent: if any control_axiom rows already
exist (active or archived) the seeder is a no-op, so operators can
edit / delete axioms via the REST surface without risking a re-seed
on the next restart.
"""
from __future__ import annotations

from typing import Any, Optional, Tuple

from loguru import logger

from .manager import MemoryManager
from .store import (
    KIND_CONTROL_AXIOM,
    SOURCE_IMPORT,
    MemoryError,
    MemoryStore,
)


# Seven L1 axioms distilled from 《工程控制论》 (Engineering Cybernetics,
# 1954) and 《论系统工程》 (On Systems Engineering, 1988). Each line is
# a *thinking primitive* the agent should run BEFORE acting — the goal
# is to give the LLM a stable cognitive checklist regardless of which
# skill / tool it ends up invoking. Kept short on purpose: the prompt
# budget is precious, and these are meant to be re-read every turn.
#
# Edit policy: operators may add / remove axioms via the REST surface.
# Re-seeding is gated on the row count being zero, so this list only
# matters on a fresh database. Adding entries here later DOES NOT
# auto-propagate; that's deliberate — once an operator has curated
# their axiom set we don't want a code update to silently overwrite
# their decisions.
DEFAULT_CONTROL_AXIOMS: Tuple[str, ...] = (
    "目标→状态→偏差→反馈→执行→校正：动手前先把这条闭环走一遍，"
    "明确目标、可观测状态、判定偏差的指标。",
    "系统可靠性 > 元件精度：单条记忆不必完美，"
    "但 cron 复盘 + 用户确认 + agent 执行 这条反馈回路必须通畅。",
    "三层递阶控制：L1 控制论原则（思考方式）"
    " → L2 抽象底层逻辑（触发条件 / 判断标准 / 失败信号）"
    " → L3 落地场景核心逻辑（触发对应工具或 skill）。",
    "综合集成（定性 + 定量）：用户经验是定性，工具调用统计是定量，"
    "agent 把两者合成模型，最后由用户确认收敛。",
    "记忆只存核心逻辑：一次性细节、聊天日志、临时上下文不进记忆体；"
    "进得来的必须是可在未来同类场景下复用的逻辑。",
    "skill 延迟梳理：skill 在创建时不要强行用控制论梳理；"
    "等到一天工作完成或阶段结束，再把 memory + skill 一起按控制论闭环复盘。",
    "先稳定再智能：已知 bug、时间漂移、检索错路径未修前不引入新能力——"
    "这是控制论里 \"先校正偏差再扩展目标\" 的直译。",
)


def seed_control_axioms(
    store: MemoryStore,
    *,
    axioms: Optional[Tuple[str, ...]] = None,
) -> int:
    """Idempotently populate the L1 control_axiom layer.

    Returns the number of rows written this call. ``0`` means the
    layer was already populated (operator-edited or seeded by an
    earlier boot) — we never overwrite what's there.

    Raises nothing in normal use: per-row failures are logged but
    don't abort the boot, because a missing axiom shouldn't kill the
    whole agent. ``MemoryError`` from the store *is* swallowed for
    the same reason; the smoke test exercises the success path
    independently.
    """
    items = axioms if axioms is not None else DEFAULT_CONTROL_AXIOMS
    if not items:
        return 0
    # Idempotency check: any existing row in this layer (active or
    # archived) is a signal that the operator has already taken
    # ownership of this layer. Bail out so the operator's curated
    # state is never overwritten.
    try:
        existing = store.list(
            kind=KIND_CONTROL_AXIOM, include_archived=True, limit=1,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[memory] axiom seed precheck failed: {}", exc)
        return 0
    if existing:
        logger.info(
            "[memory] control_axiom layer already populated"
            " ({} pre-existing); skipping seed", len(existing),
        )
        return 0
    written = 0
    for text in items:
        try:
            store.add(
                text,
                kind=KIND_CONTROL_AXIOM,
                source=SOURCE_IMPORT,
                pinned=True,  # sacred: LRU sweep must not touch these
            )
            written += 1
        except MemoryError as exc:
            logger.warning("[memory] axiom seed rejected: {!r} ({})", text, exc)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[memory] axiom seed crashed: {!r} ({})", text, exc)
    if written:
        logger.info("[memory] seeded {} L1 control_axiom row(s)", written)
    return written


def build_memory_subsystem(
    settings: Any,
    *,
    redis_backend: Optional[Any],
) -> Tuple[MemoryStore, MemoryManager]:
    """Build ``(memory_store, memory_manager)``.

    The session-context provider is wired only when
    ``memory_session_context_enabled`` is True (default). Operators
    can disable it to bring up a fresh deployment without leaking
    prior turns from the JSONL archive.
    """
    memory_store = MemoryStore(
        max_entries=settings.memory_max_entries,
        max_entry_chars=settings.memory_max_entry_chars,
    )
    memory_manager = MemoryManager(
        memory_store,
        max_user_facts_in_prompt=settings.memory_max_user_facts_in_prompt,
        max_agent_notes_in_prompt=settings.memory_max_agent_notes_in_prompt,
        max_prefetch_results=settings.memory_max_prefetch_results,
        keyword_prefetch_enabled=settings.memory_keyword_prefetch_enabled,
    )
    if settings.memory_session_context_enabled:
        from .session_context import SessionContextProvider
        memory_manager.add_provider(
            SessionContextProvider(
                settings.workspace_dir / "memory" / "session_context.jsonl",
                max_turns=settings.memory_session_context_max_turns,
                max_chars=settings.memory_session_context_max_chars,
                context_window_tokens=settings.llm_context_window_tokens,
                token_budget_ratio=settings.memory_session_context_token_budget_ratio,
                redis_backend=redis_backend,
                redis_ttl_seconds=settings.redis_session_ttl_seconds,
            )
        )
    # v0.43: seed L1 control-theory axioms on first boot (no-op on
    # subsequent boots once the operator has any control_axiom rows).
    seed_control_axioms(memory_store)
    logger.info("memory store ready ({} max entries)", settings.memory_max_entries)
    return memory_store, memory_manager
