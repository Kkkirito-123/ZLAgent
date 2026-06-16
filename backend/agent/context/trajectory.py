"""Trajectory compression (v0.15).

Long-running conversations — especially after a few cron-driven
``read_url`` / ``web_search`` chains or a delegate fork — accumulate
a lot of bulky tool messages that quickly push the LLM context past
the comfortable budget. Hermes Agent solves this with a two-phase
``trajectory_compressor``: first trim verbose old tool results, then
collapse whole middle rounds into a single ``system`` summary while
keeping the most-recent rounds intact.

This module is the ZLAgent equivalent. It is intentionally:

* **Stateless** — every call inspects ``history`` and decides anew.
  No hidden caches; the LLM message list is the source of truth.
* **Mutate-in-place** — callers (the agent loop) keep their reference
  to ``history`` and append to it after the call returns. We splice
  the same list rather than returning a new one so confirmation /
  review forks pick up the trimmed state automatically.
* **Pairing-safe** — when collapsing rounds we always cut on user-
  message boundaries so an ``assistant.tool_calls`` entry is never
  separated from its ``role=tool`` follow-up. Otherwise the OpenAI
  tool API would reject the next request with HTTP 400.

Threshold defaults are conservative: 32 KiB of total content (≈ 8k
tokens at ~4 chars/token) trims; 24 KiB after phase 1 still triggers
phase 2. Tune via :class:`backend.core.config.Settings` or per-call.

The function never raises into the caller — pathological histories
return a no-op :class:`CompressionStats` and log at WARNING.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from loguru import logger

from ...llm import LLMMessage


# Marker prefix on the synthetic system message that replaces collapsed
# rounds. Smoke tests grep for it; do not change without updating them.
COMPRESSION_MARKER = "[trajectory_compressed]"

# Tool-call / tool-result pairing is enforced by every OpenAI-compatible
# provider. Phase 2 must cut on user-message boundaries to stay valid.
DEFAULT_MAX_CHARS = 32 * 1024
DEFAULT_KEEP_RECENT_ROUNDS = 2
DEFAULT_TRUNCATED_TOOL_CHARS = 200
DEFAULT_KEEP_LAST_TOOL_RESULTS = 3


@dataclass(slots=True)
class CompressionStats:
    """Telemetry for one ``compress_history`` invocation."""

    compressed: bool = False
    phase: str = "none"  # "none" | "truncate" | "drop_rounds" | "both"
    before_chars: int = 0
    after_chars: int = 0
    before_messages: int = 0
    after_messages: int = 0
    rounds_dropped: int = 0
    tool_messages_truncated: int = 0


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def estimate_chars(history: list[LLMMessage]) -> int:
    """Approximate the LLM context size by char count.

    We count ``content`` + ``reasoning_content`` + every tool_call's
    ``name`` / ``arguments``. This is intentionally loose (chars, not
    tokens) — the goal is a stable signal for "this history is getting
    big", not perfect token accounting.
    """
    total = 0
    for msg in history:
        total += len(msg.content or "")
        if msg.reasoning_content:
            total += len(msg.reasoning_content)
        if msg.tool_calls:
            for tc in msg.tool_calls:
                total += len(tc.arguments or "") + len(tc.name or "")
    return total


def compress_history(
    history: list[LLMMessage],
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    keep_recent_rounds: int = DEFAULT_KEEP_RECENT_ROUNDS,
    truncated_tool_chars: int = DEFAULT_TRUNCATED_TOOL_CHARS,
    keep_last_tool_results: int = DEFAULT_KEEP_LAST_TOOL_RESULTS,
) -> CompressionStats:
    """Compress ``history`` in place to fit roughly within ``max_chars``.

    Two-phase strategy:

    1. **Truncate old tool results** — the highest-bandwidth-per-value
       messages. We keep the most recent ``keep_last_tool_results``
       intact (those drive the next LLM decision) and squash older
       ones to ``truncated_tool_chars`` plus a marker.
    2. **Drop full middle rounds** — only if phase 1 wasn't enough.
       A "round" starts at a user message and runs until the next user
       message. We keep the first round (initial user prompt is the
       grounding intent) and the last ``keep_recent_rounds``, replacing
       the middle with a single synthetic ``system`` summary.

    Returns a :class:`CompressionStats` describing what happened. The
    caller can log / surface it without needing a second pass.

    Defensive: any internal exception is swallowed to a no-op stats
    object — the agent loop must never break because compression
    misbehaved.
    """
    before_chars = estimate_chars(history)
    before_messages = len(history)
    stats = CompressionStats(
        before_chars=before_chars,
        after_chars=before_chars,
        before_messages=before_messages,
        after_messages=before_messages,
    )
    if before_chars <= max_chars or not history:
        return stats

    try:
        # ── Phase 1 ──────────────────────────────────────────────────
        truncated_count = _truncate_old_tool_messages(
            history,
            truncated_tool_chars=truncated_tool_chars,
            keep_last_n=keep_last_tool_results,
        )
        stats.tool_messages_truncated = truncated_count
        if truncated_count:
            stats.compressed = True
            stats.phase = "truncate"

        if estimate_chars(history) <= max_chars:
            stats.after_chars = estimate_chars(history)
            stats.after_messages = len(history)
            return stats

        # ── Phase 2 ──────────────────────────────────────────────────
        rounds_dropped = _drop_middle_rounds(
            history, keep_recent_rounds=keep_recent_rounds,
        )
        if rounds_dropped:
            stats.rounds_dropped = rounds_dropped
            stats.compressed = True
            stats.phase = "drop_rounds" if stats.phase == "none" else "both"
    except Exception as exc:  # noqa: BLE001 — compression must not crash the loop
        logger.warning("[trajectory] compression failed mid-pass: {}", exc)

    stats.after_chars = estimate_chars(history)
    stats.after_messages = len(history)
    return stats


# ---------------------------------------------------------------------------
# Phase 1 — tool-result truncation
# ---------------------------------------------------------------------------


def _truncate_old_tool_messages(
    history: list[LLMMessage],
    *,
    truncated_tool_chars: int,
    keep_last_n: int,
) -> int:
    """Replace older ``role='tool'`` content with a short stub.

    Returns the number of messages actually trimmed. The most recent
    ``keep_last_n`` tool results are preserved verbatim so the very
    next LLM decision still has its working memory.
    """
    tool_indices = [
        i for i, m in enumerate(history) if m.role == "tool"
    ]
    if len(tool_indices) <= keep_last_n:
        return 0
    cutoff = len(tool_indices) - keep_last_n
    trimmed = 0
    for idx in tool_indices[:cutoff]:
        msg = history[idx]
        body = msg.content or ""
        if len(body) <= truncated_tool_chars:
            continue
        head = body[:truncated_tool_chars]
        # Build a fresh message preserving routing fields. We need
        # the original ``tool_call_id`` / ``name`` so the assistant
        # message that emitted the call still pairs cleanly.
        history[idx] = LLMMessage(
            role="tool",
            content=(
                head
                + f"\n... {COMPRESSION_MARKER} tool result truncated"
                f" ({len(body)} → {truncated_tool_chars} chars)"
            ),
            name=msg.name,
            tool_call_id=msg.tool_call_id,
        )
        trimmed += 1
    return trimmed


# ---------------------------------------------------------------------------
# Phase 2 — round dropping
# ---------------------------------------------------------------------------


def _drop_middle_rounds(
    history: list[LLMMessage],
    *,
    keep_recent_rounds: int,
) -> int:
    """Collapse middle rounds into a single ``system`` summary.

    A "round" is the slice of ``history`` from one user message up to
    (but not including) the next user message. We keep:

    * Every leading ``role='system'`` message (memory snapshot,
      prefetch block, base prompt) — these define the model's
      identity for the whole conversation.
    * The first user message + its accompanying assistant / tool
      replies — the grounding intent.
    * The most recent ``keep_recent_rounds`` rounds — what the next
      LLM call will reason over.

    Everything in between is replaced with a single system message
    that names the dropped tools and user phrasing in a few hundred
    chars. Returns the number of rounds collapsed (0 if a no-op).
    """
    user_indices = [i for i, m in enumerate(history) if m.role == "user"]
    # Need at least: 1 (kept first) + keep_recent_rounds + 1 (something
    # to drop in between) for compression to make sense.
    if len(user_indices) < keep_recent_rounds + 2:
        return 0

    keep_first_user = user_indices[0]
    keep_tail_start = user_indices[-keep_recent_rounds]
    # First round runs from keep_first_user (inclusive) until the next
    # user message (exclusive). Hence the slice we drop is
    # [first_round_end + 1, keep_tail_start).
    first_round_end = user_indices[1] - 1
    if first_round_end >= keep_tail_start:
        return 0
    dropped_block = history[first_round_end + 1 : keep_tail_start]
    if not dropped_block:
        return 0
    rounds_dropped = len(user_indices) - 1 - keep_recent_rounds
    if rounds_dropped < 1:
        return 0

    summary = _summarize_dropped(dropped_block, rounds_dropped=rounds_dropped)
    sys_msg = LLMMessage(role="system", content=summary)
    history[first_round_end + 1 : keep_tail_start] = [sys_msg]
    return rounds_dropped


def _summarize_dropped(
    dropped: list[LLMMessage], *, rounds_dropped: int,
) -> str:
    """Build the synthetic system message that stands in for ``dropped``.

    Captures: tool names invoked (with counts), user phrasings, and a
    head/tail of any final assistant text. Hard-capped at 1500 chars
    so even an extremely long pruned block produces a compact stub.
    """
    tool_counts: dict[str, int] = {}
    user_phrases: list[str] = []
    assistant_tails: list[str] = []
    for msg in dropped:
        if msg.role == "user":
            phrase = (msg.content or "").strip().splitlines()[:1]
            if phrase:
                user_phrases.append(phrase[0][:120])
        elif msg.role == "assistant":
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    name = tc.name or "?"
                    tool_counts[name] = tool_counts.get(name, 0) + 1
            else:
                tail = (msg.content or "").strip()
                if tail:
                    assistant_tails.append(tail[:120])

    parts: list[str] = [
        COMPRESSION_MARKER,
        f"折叠了 {rounds_dropped} 个中间轮次（共 {len(dropped)} 条消息）",
    ]
    if tool_counts:
        rendered = ", ".join(
            f"{name}×{count}" for name, count in
            sorted(tool_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        )
        parts.append(f"调用过的工具: {rendered}")
    if user_phrases:
        parts.append("用户提到: " + " | ".join(user_phrases[:5]))
    if assistant_tails:
        parts.append("助手回复片段: " + " | ".join(assistant_tails[:3]))
    parts.append(
        "注意：以上仅为压缩摘要。原始详情已舍弃以节省上下文，"
        "若需要请重新向用户确认或重跑相关工具。"
    )
    body = "\n".join(parts)
    if len(body) > 1500:
        body = body[:1500] + "\n... [summary truncated]"
    return body
