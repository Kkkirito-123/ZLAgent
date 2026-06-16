from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from loguru import logger

from ...llm import LLMMessage
from .summary import SummaryCompressor
from .trajectory import (
    COMPRESSION_MARKER,
    DEFAULT_KEEP_LAST_TOOL_RESULTS,
    DEFAULT_KEEP_RECENT_ROUNDS,
    DEFAULT_MAX_CHARS,
    DEFAULT_TRUNCATED_TOOL_CHARS,
    CompressionStats,
    compress_history,
    estimate_chars,
)

DEFAULT_CHARS_PER_TOKEN = 4
DEFAULT_FAILURE_COOLDOWN_SECONDS = 30.0
DEFAULT_MIN_COMPRESSION_INTERVAL_SECONDS = 2.0
DEFAULT_MAX_CONSECUTIVE_FAILURES = 3


@dataclass(slots=True)
class ContextEngineState:
    last_compressed_at: float = 0.0
    last_attempt_at: float = 0.0
    consecutive_failures: int = 0
    compact_count: int = 0
    last_summary: str = ""


@dataclass(slots=True)
class ContextEngineStats:
    compressed: bool = False
    skipped_reason: str = ""
    phase: str = "none"
    before_chars: int = 0
    after_chars: int = 0
    before_tokens: int = 0
    after_tokens: int = 0
    before_messages: int = 0
    after_messages: int = 0
    rounds_dropped: int = 0
    tool_messages_truncated: int = 0
    micro_compacted_messages: int = 0
    consecutive_failures: int = 0
    # telemetry for the LLM-summary phase. ``summary_used`` is
    # True only when the summary path produced a real compression;
    # ``summary_skipped_reason`` carries the diagnostic for both
    # "didn't try" and "tried but failed" cases.
    summary_used: bool = False
    summary_skipped_reason: str = ""
    summary_chars: int = 0


class ContextEngine:
    def __init__(
        self,
        *,
        max_chars: int = DEFAULT_MAX_CHARS,
        keep_recent_rounds: int = DEFAULT_KEEP_RECENT_ROUNDS,
        keep_last_tool_results: int = DEFAULT_KEEP_LAST_TOOL_RESULTS,
        truncated_tool_chars: int = DEFAULT_TRUNCATED_TOOL_CHARS,
        chars_per_token: int = DEFAULT_CHARS_PER_TOKEN,
        min_compression_interval_seconds: float = DEFAULT_MIN_COMPRESSION_INTERVAL_SECONDS,
        failure_cooldown_seconds: float = DEFAULT_FAILURE_COOLDOWN_SECONDS,
        max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES,
        summary_compressor: Optional[SummaryCompressor] = None,
        summary_threshold_chars: Optional[int] = None,
    ) -> None:
        self._max_chars = max_chars
        self._keep_recent_rounds = keep_recent_rounds
        self._keep_last_tool_results = keep_last_tool_results
        self._truncated_tool_chars = truncated_tool_chars
        self._chars_per_token = max(1, chars_per_token)
        self._min_interval = max(0.0, min_compression_interval_seconds)
        self._failure_cooldown = max(0.0, failure_cooldown_seconds)
        self._max_failures = max(1, max_consecutive_failures)
        # optional LLM-summary phase. When ``None`` the engine
        # behaves exactly as v0.16 did (micro + deterministic). When set,
        # ``maybe_compress_async`` will try the summary phase between
        # micro and the deterministic round-drop fallback.
        self._summary_compressor = summary_compressor
        # Summary kicks in only after micro+ legacy didn't bring history
        # under ``summary_threshold_chars``. Defaults to 1.5× ``max_chars``
        # so trivial overshoots stay on the cheap deterministic path.
        self._summary_threshold = (
            summary_threshold_chars
            if summary_threshold_chars is not None
            else int(max_chars * 1.5)
        )
        self.state = ContextEngineState()

    def estimate_tokens(self, history: list[LLMMessage]) -> int:
        return max(1, estimate_chars(history) // self._chars_per_token)

    def maybe_compress(self, history: list[LLMMessage]) -> ContextEngineStats:
        before_chars = estimate_chars(history)
        before_messages = len(history)
        stats = ContextEngineStats(
            before_chars=before_chars,
            after_chars=before_chars,
            before_tokens=max(1, before_chars // self._chars_per_token),
            after_tokens=max(1, before_chars // self._chars_per_token),
            before_messages=before_messages,
            after_messages=before_messages,
            consecutive_failures=self.state.consecutive_failures,
        )
        if before_chars <= self._max_chars or not history:
            stats.skipped_reason = "under_budget"
            return stats

        now = time.monotonic()
        if self.state.consecutive_failures >= self._max_failures:
            if now - self.state.last_attempt_at < self._failure_cooldown:
                stats.skipped_reason = "failure_cooldown"
                return stats
        if self.state.last_compressed_at and now - self.state.last_compressed_at < self._min_interval:
            stats.skipped_reason = "anti_thrash"
            return stats

        self.state.last_attempt_at = now
        try:
            micro_count = self._micro_compact(history)
            legacy_stats = compress_history(
                history,
                max_chars=self._max_chars,
                keep_recent_rounds=self._keep_recent_rounds,
                truncated_tool_chars=self._truncated_tool_chars,
                keep_last_tool_results=self._keep_last_tool_results,
            )
            stats.micro_compacted_messages = micro_count
            stats.tool_messages_truncated = legacy_stats.tool_messages_truncated
            stats.rounds_dropped = legacy_stats.rounds_dropped
            stats.phase = self._phase(micro_count, legacy_stats)
            stats.after_chars = estimate_chars(history)
            stats.after_tokens = max(1, stats.after_chars // self._chars_per_token)
            stats.after_messages = len(history)
            stats.compressed = micro_count > 0 or legacy_stats.compressed
            if stats.compressed:
                self.state.consecutive_failures = 0
                self.state.last_compressed_at = now
                self.state.compact_count += 1
                self.state.last_summary = self._summarize_stats(stats)
            else:
                stats.skipped_reason = "no_effect"
            stats.consecutive_failures = self.state.consecutive_failures
            return stats
        except Exception as exc:  # noqa: BLE001
            self.state.consecutive_failures += 1
            stats.consecutive_failures = self.state.consecutive_failures
            stats.skipped_reason = "error"
            stats.after_chars = estimate_chars(history)
            stats.after_tokens = max(1, stats.after_chars // self._chars_per_token)
            stats.after_messages = len(history)
            logger.warning("[context] compression failed: {}", exc)
            return stats

    async def maybe_compress_async(
        self, history: list[LLMMessage]
    ) -> ContextEngineStats:
        """Async variant adding the v0.21 LLM-summary phase.

        Sequence:

        1. Always run :meth:`_micro_compact` (cheap; no LLM).
        2. If a :class:`SummaryCompressor` is configured AND the history
           is still above ``summary_threshold_chars``, attempt the
           LLM-summary phase. On success we skip the deterministic
           round-drop entirely — the LLM summary is the better artifact.
        3. On summary skip / failure / not-configured, fall back to
           :func:`compress_history` (the deterministic round-drop the
           sync path uses).

        Mirrors :meth:`maybe_compress` for state bookkeeping (cooldown,
        anti-thrash, consecutive_failures) so the two entry points
        share the same back-pressure semantics.
        """
        before_chars = estimate_chars(history)
        before_messages = len(history)
        stats = ContextEngineStats(
            before_chars=before_chars,
            after_chars=before_chars,
            before_tokens=max(1, before_chars // self._chars_per_token),
            after_tokens=max(1, before_chars // self._chars_per_token),
            before_messages=before_messages,
            after_messages=before_messages,
            consecutive_failures=self.state.consecutive_failures,
        )
        if before_chars <= self._max_chars or not history:
            stats.skipped_reason = "under_budget"
            return stats

        now = time.monotonic()
        if self.state.consecutive_failures >= self._max_failures:
            if now - self.state.last_attempt_at < self._failure_cooldown:
                stats.skipped_reason = "failure_cooldown"
                return stats
        if (
            self.state.last_compressed_at
            and now - self.state.last_compressed_at < self._min_interval
        ):
            stats.skipped_reason = "anti_thrash"
            return stats
        self.state.last_attempt_at = now

        try:
            micro_count = self._micro_compact(history)
            stats.micro_compacted_messages = micro_count
        except Exception as exc:  # noqa: BLE001
            logger.warning("[context] micro_compact failed: {}", exc)
            self.state.consecutive_failures += 1
            stats.consecutive_failures = self.state.consecutive_failures
            stats.skipped_reason = "error"
            stats.after_chars = estimate_chars(history)
            stats.after_tokens = max(1, stats.after_chars // self._chars_per_token)
            stats.after_messages = len(history)
            return stats

        # Decide whether to invoke the LLM summary phase.
        post_micro_chars = estimate_chars(history)
        summary_attempted = False
        summary_compressed = False
        if self._summary_compressor is None or not self._summary_compressor.configured:
            stats.summary_skipped_reason = "not_configured"
        elif post_micro_chars <= self._summary_threshold:
            stats.summary_skipped_reason = "under_summary_threshold"
        else:
            summary_attempted = True
            summary_result = await self._summary_compressor.try_compress(history)
            if summary_result.compressed:
                summary_compressed = True
                stats.summary_used = True
                stats.summary_chars = summary_result.summary_chars
                stats.rounds_dropped = summary_result.rounds_dropped
            else:
                stats.summary_skipped_reason = (
                    summary_result.skipped_reason or "summary_no_op"
                )

        legacy_used = False
        if not summary_compressed:
            try:
                legacy_stats = compress_history(
                    history,
                    max_chars=self._max_chars,
                    keep_recent_rounds=self._keep_recent_rounds,
                    truncated_tool_chars=self._truncated_tool_chars,
                    keep_last_tool_results=self._keep_last_tool_results,
                )
                stats.tool_messages_truncated = legacy_stats.tool_messages_truncated
                # Only overwrite rounds_dropped from legacy when summary
                # didn't already supply a value.
                if not summary_compressed:
                    stats.rounds_dropped = legacy_stats.rounds_dropped
                legacy_used = legacy_stats.compressed
            except Exception as exc:  # noqa: BLE001
                logger.warning("[context] legacy compression failed: {}", exc)

        # Phase string + final accounting.
        phase_parts: list[str] = []
        if stats.micro_compacted_messages:
            phase_parts.append("micro")
        if summary_compressed:
            phase_parts.append("summary")
        elif legacy_used:
            phase_parts.append("drop_rounds")
        stats.phase = "+".join(phase_parts) if phase_parts else "none"

        stats.after_chars = estimate_chars(history)
        stats.after_tokens = max(1, stats.after_chars // self._chars_per_token)
        stats.after_messages = len(history)
        stats.compressed = bool(
            stats.micro_compacted_messages or summary_compressed or legacy_used
        )
        if stats.compressed:
            self.state.consecutive_failures = 0
            self.state.last_compressed_at = now
            self.state.compact_count += 1
            self.state.last_summary = self._summarize_async_stats(stats)
        else:
            stats.skipped_reason = "no_effect"
        # When summary was attempted but failed, surface that failure
        # via consecutive_failures so the global cooldown engages too.
        if summary_attempted and not summary_compressed and not legacy_used:
            self.state.consecutive_failures += 1
        stats.consecutive_failures = self.state.consecutive_failures
        return stats

    @staticmethod
    def _summarize_async_stats(stats: "ContextEngineStats") -> str:
        return (
            f"phase={stats.phase} chars={stats.before_chars}->{stats.after_chars}"
            f" rounds_dropped={stats.rounds_dropped}"
            f" tool_truncated={stats.tool_messages_truncated}"
            f" micro={stats.micro_compacted_messages}"
            f" summary_used={stats.summary_used}"
        )

    def _micro_compact(self, history: list[LLMMessage]) -> int:
        compacted = 0
        tool_indices = [i for i, msg in enumerate(history) if msg.role == "tool"]
        if len(tool_indices) <= self._keep_last_tool_results:
            return 0
        protected = set(tool_indices[-self._keep_last_tool_results:])
        for idx in tool_indices:
            if idx in protected:
                continue
            msg = history[idx]
            content = msg.content or ""
            new_content = self._compact_tool_content(content)
            if new_content != content:
                history[idx] = LLMMessage(
                    role="tool",
                    content=new_content,
                    name=msg.name,
                    tool_call_id=msg.tool_call_id,
                )
                compacted += 1
        return compacted

    def _compact_tool_content(self, content: str) -> str:
        if not content or COMPRESSION_MARKER in content:
            return content
        limit = max(self._truncated_tool_chars * 2, self._truncated_tool_chars + 200)
        if len(content) <= limit:
            return content
        head = content[: self._truncated_tool_chars]
        tail_budget = max(0, self._truncated_tool_chars // 2)
        tail = content[-tail_budget:] if tail_budget else ""
        omitted = len(content) - len(head) - len(tail)
        body = f"{head}\n... {COMPRESSION_MARKER} micro compacted tool result; omitted {omitted} chars"
        if tail:
            body += f"\n... tail:\n{tail}"
        return body

    @staticmethod
    def _phase(micro_count: int, legacy_stats: CompressionStats) -> str:
        phases: list[str] = []
        if micro_count:
            phases.append("micro")
        if legacy_stats.phase != "none":
            phases.append(legacy_stats.phase)
        return "+".join(phases) if phases else "none"

    @staticmethod
    def _summarize_stats(stats: ContextEngineStats) -> str:
        return (
            f"phase={stats.phase} chars={stats.before_chars}->{stats.after_chars} "
            f"tokens≈{stats.before_tokens}->{stats.after_tokens} "
            f"msgs={stats.before_messages}->{stats.after_messages} "
            f"tools={stats.tool_messages_truncated} micro={stats.micro_compacted_messages} "
            f"rounds={stats.rounds_dropped}"
        )
