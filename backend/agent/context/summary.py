"""LLM-driven summary phase for ContextEngine.

Sits between the cheap micro-compaction layer and the deterministic
round-drop fallback. Asks an auxiliary LLM call to fold middle rounds
into a 4-section Chinese summary so we lose context only as a last
resort.

Key pieces:

* :class:`SummaryCompressor` — stateful compressor with cooldown.
* :class:`SummaryResult`     — outcome row (compressed / skipped /
                               failure reason).
* :data:`SUMMARY_MARKER`     — string injected into the synthetic
                               system message so downstream stages
                               (other compressors, smoke tests) can
                               recognise an already-compressed slot.

The compressor never raises into its caller — every failure path
returns a ``SummaryResult(compressed=False, skipped_reason=...)`` and
records ``last_failure_reason`` + a cooldown timestamp so the engine
can stop retrying for a while.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from loguru import logger

from ...llm import LLMClient, LLMMessage
from ...core.redact import redact_text


SUMMARY_MARKER = "[ZLAgent compressed summary]"

DEFAULT_KEEP_RECENT_ROUNDS = 2
DEFAULT_SUMMARY_MAX_CHARS = 4 * 1024
DEFAULT_FAILURE_COOLDOWN_SECONDS = 60.0
DEFAULT_MIN_ROUNDS_TO_COMPRESS = 2


_SYSTEM_PROMPT = (
    "你是一个压缩助手。我会给你一段已发生的对话历史以及若干长期记忆。"
    "请用 5 个固定小节总结，每节一段简短中文，不要复述全部细节：\n"
    "## 当前任务\n## 已经做的决定\n## 使用的工具与文件\n"
    "## 已解决的问题\n## 剩余工作 / pitfall\n\n"
    "约束：\n"
    "1. 严格输出上述 5 个小节标题，不能新增；\n"
    "2. 每节 1-3 行，写不出来时填\"暂无\"；\n"
    "3. 不要重复整段原文；\n"
    "4. 不要泄露 API key / token；\n"
    "5. 整体长度 ≤ 1.5KB。"
)


@dataclass(slots=True)
class SummaryResult:
    compressed: bool = False
    skipped_reason: str = ""
    summary_chars: int = 0
    rounds_dropped: int = 0


# Type alias for the optional chat function. Receives the full message
# list (system + user-with-transcript) and must return an object with
# a ``.content`` attribute (LLMResponse-shaped). Async only.
ChatFn = Callable[[list[LLMMessage]], Awaitable[object]]


class SummaryCompressor:
    """Async LLM-summary compressor with cooldown + memory hook.

    Two ways to wire the LLM:

    * ``llm=`` — pass a configured :class:`LLMClient`; we use its
      ``chat`` method.
    * ``chat_fn=`` — pass any async callable for tests / alternative
      clients. Mutually exclusive with ``llm``.

    ``memory_pre_compress`` is a sync callable returning a plain-text
    block of memories to splice into the user prompt before the
    transcript. Used to keep cross-session preferences alive across
    a compression boundary.
    """

    def __init__(
        self,
        *,
        llm: Optional[LLMClient] = None,
        chat_fn: Optional[ChatFn] = None,
        keep_recent_rounds: int = DEFAULT_KEEP_RECENT_ROUNDS,
        summary_max_chars: int = DEFAULT_SUMMARY_MAX_CHARS,
        failure_cooldown_seconds: float = DEFAULT_FAILURE_COOLDOWN_SECONDS,
        min_rounds_to_compress: int = DEFAULT_MIN_ROUNDS_TO_COMPRESS,
        memory_pre_compress: Optional[Callable[[list[LLMMessage]], str]] = None,
    ) -> None:
        if llm is not None and chat_fn is not None:
            raise ValueError("pass at most one of llm= / chat_fn=")
        self._llm = llm
        self._chat_fn: Optional[ChatFn] = chat_fn
        self._keep_recent_rounds = max(1, int(keep_recent_rounds))
        self._summary_max_chars = max(256, int(summary_max_chars))
        self._failure_cooldown = max(0.0, float(failure_cooldown_seconds))
        self._min_rounds_to_compress = max(1, int(min_rounds_to_compress))
        self._memory_pre_compress = memory_pre_compress

        self._last_failure_at: float = 0.0
        self.last_failure_reason: str = ""

    # ------------------------------------------------------------------
    @property
    def configured(self) -> bool:
        if self._chat_fn is not None:
            return True
        if self._llm is None:
            return False
        try:
            return bool(self._llm.configured)
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------------
    async def try_compress(
        self, history: list[LLMMessage]
    ) -> SummaryResult:
        """Attempt a summary compression. Returns a :class:`SummaryResult`."""
        if not self.configured:
            return SummaryResult(skipped_reason="llm_not_configured")

        # Cooldown gate.
        if self._last_failure_at and self._failure_cooldown > 0.0:
            elapsed = time.monotonic() - self._last_failure_at
            if elapsed < self._failure_cooldown:
                return SummaryResult(skipped_reason="failure_cooldown")

        rounds = _identify_rounds(history)
        if len(rounds) < self._min_rounds_to_compress + self._keep_recent_rounds:
            return SummaryResult(skipped_reason="not_enough_rounds")

        # Slice: ``head`` = leading system messages, ``middle`` = rounds
        # we'll compress, ``tail`` = the last N rounds we keep verbatim.
        head_end = rounds[0].start if rounds else 0
        head = history[:head_end]

        # Anything before the first round but after a system message
        # (rare, but possible if we have multiple leading system msgs).
        keep_tail_start = rounds[-self._keep_recent_rounds].start
        middle = history[head_end:keep_tail_start]
        tail = history[keep_tail_start:]
        if not middle:
            return SummaryResult(skipped_reason="not_enough_rounds")

        # Build the user prompt: optional memory block + transcript text.
        memory_block = ""
        if self._memory_pre_compress is not None:
            try:
                memory_block = self._memory_pre_compress(history) or ""
            except Exception as exc:  # noqa: BLE001
                logger.debug("[summary] memory_pre_compress raised: {}", exc)
                memory_block = ""

        transcript = _render_transcript(middle)
        user_prompt_parts: list[str] = []
        if memory_block.strip():
            user_prompt_parts.append("# 长期记忆\n" + memory_block.strip())
        user_prompt_parts.append("# 待压缩的对话历史\n" + transcript)
        # Redact secrets that might have leaked into the transcript
        # before they reach the auxiliary LLM.
        user_prompt = redact_text("\n\n".join(user_prompt_parts)) or ""

        prompt_messages = [
            LLMMessage(role="system", content=_SYSTEM_PROMPT),
            LLMMessage(role="user", content=user_prompt),
        ]

        try:
            response = await self._invoke(prompt_messages)
        except Exception as exc:  # noqa: BLE001
            self._note_failure(f"{type(exc).__name__}: {exc}")
            return SummaryResult(skipped_reason="llm_error")

        summary_text = (getattr(response, "content", "") or "").strip()
        if not summary_text:
            self._note_failure("empty content from summarizer")
            return SummaryResult(skipped_reason="empty_summary")

        # Truncate ridiculously long summaries — the whole point is a
        # compact stub.
        if len(summary_text) > self._summary_max_chars:
            summary_text = (
                summary_text[: self._summary_max_chars]
                + "\n[...summary truncated]"
            )

        synthetic = LLMMessage(
            role="system",
            content=f"{SUMMARY_MARKER}\n{summary_text}",
        )

        # Rebuild ``history`` in place so callers holding the same list
        # see the change.
        rounds_dropped = len(rounds) - self._keep_recent_rounds
        history.clear()
        history.extend(head)
        history.append(synthetic)
        history.extend(tail)

        return SummaryResult(
            compressed=True,
            summary_chars=len(summary_text),
            rounds_dropped=max(0, rounds_dropped),
        )

    # ------------------------------------------------------------------
    async def _invoke(self, messages: list[LLMMessage]) -> object:
        if self._chat_fn is not None:
            return await self._chat_fn(messages)
        assert self._llm is not None
        return await self._llm.chat(messages, tools=None)

    def _note_failure(self, reason: str) -> None:
        self.last_failure_reason = reason or "unknown"
        self._last_failure_at = time.monotonic()
        logger.warning("[summary] compression failed: {}", reason)


# -- Helpers ---------------------------------------------------------------


@dataclass(slots=True)
class _Round:
    start: int
    end: int  # exclusive


def _identify_rounds(history: list[LLMMessage]) -> list[_Round]:
    """Split ``history`` into rounds.

    A round starts on a ``user`` message and includes every following
    ``assistant`` / ``tool`` message until the next user message. Leading
    system messages are NOT part of any round (they're the "head" the
    caller keeps verbatim).
    """
    rounds: list[_Round] = []
    current_start: Optional[int] = None
    for idx, msg in enumerate(history):
        if msg.role == "user":
            if current_start is not None:
                rounds.append(_Round(start=current_start, end=idx))
            current_start = idx
        elif msg.role == "system" and current_start is None:
            # Still in the leading system block.
            continue
    if current_start is not None:
        rounds.append(_Round(start=current_start, end=len(history)))
    return rounds


def _render_transcript(messages: list[LLMMessage]) -> str:
    """Cheap text rendering of a slice of history for the summariser."""
    lines: list[str] = []
    for m in messages:
        role = m.role
        content = (m.content or "").strip()
        if role == "tool":
            tool_name = m.name or "tool"
            head = content[:1200]
            tail = "" if len(content) <= 1200 else f"\n[...{len(content)-1200} chars omitted]"
            lines.append(f"[tool:{tool_name}]\n{head}{tail}")
        else:
            lines.append(f"[{role}]\n{content}")
    return "\n\n".join(lines)


__all__ = [
    "SUMMARY_MARKER",
    "SummaryCompressor",
    "SummaryResult",
]
