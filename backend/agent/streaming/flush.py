"""Token-level streaming dispatch helper (v0.37.9).

The agent loop hands a :class:`StreamFlusher` to ``LLMClient.chat`` as a
``stream_callback`` while a final, no-tools LLM call is in flight. The
flusher accumulates ``content`` deltas and dispatches partial
``OutgoingMessage`` chunks to the IM gateway as soon as either of two
thresholds is hit:

* **flush_chars** — buffered characters reach this many. Default 200.
  Smaller floods IM rate-limits; larger defeats the typing-effect.

* **flush_interval_ms** — milliseconds since the last flush exceed this
  AND we already have at least ``min_chars`` characters in the buffer.
  Default 1500 ms / 80 chars. The min-chars floor stops the very first
  token from being sent solo (most providers emit single Chinese
  characters one at a time, and 80 chars is roughly one short
  paragraph — a reasonable atomic unit on微信).

Why a separate class
--------------------

* Keeps :mod:`backend.llm.openai_compatible` ignorant of IM concerns;
  it just calls ``await stream_callback(text)`` per chunk.
* Encapsulates the multi-message rate-limiting math so the agent loop
  stays readable.
* Holds the full aggregated text so the caller can still write the
  result into the wiki cache + memory + return it as the loop outcome
  (``None`` final_text would defeat both downstream consumers).

Design choices
--------------

* **Multi-message dispatch, not edit-in-place.** A real
  ``edit_message`` API requires per-gateway support and a way to
  surface the upstream message id back to the agent loop. We start
  with the channel-portable strategy (multiple short sends) and let
  per-gateway adapters opt into edit-mode in a later release. Microsoft
  Teams / 飞书 / Telegram all support it; 微信公众号 do
  not.

* **Best-effort dispatch.** If the gateway raises while we're trying
  to flush a partial chunk, the failure is logged and **swallowed** —
  the surrounding LLM call must keep running so the user still gets a
  final answer (which the agent loop will fall back to dispatching as
  one big message). Streaming is a "nice to have", never a hard
  dependency.

* **Idempotent finalize.** ``finalize()`` is safe to call multiple
  times; subsequent calls become no-ops. The agent loop calls it from
  the success path AND from the exception/cancel path so we never lose
  a buffered tail.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from loguru import logger

from ...gateways.base import DeliveryTarget, OutgoingMessage

DispatchFn = Callable[[OutgoingMessage], Awaitable[None]]


@dataclass(slots=True)
class StreamFlushStats:
    """Telemetry surfaced after a flusher run.

    ``flush_count`` is the number of partial sends actually pushed
    to the gateway. ``aggregate_chars`` is the total visible text
    after the run — populated whether or not any flush happened, so
    the agent loop can write it to the wiki cache + memory.
    """

    flush_count: int = 0
    suppressed_count: int = 0
    aggregate_chars: int = 0
    finalized: bool = False
    finalize_caused_flush: bool = False


class StreamFlusher:
    """Bufferred token-stream dispatcher with two flush triggers.

    Constructor arguments mirror the corresponding Settings keys so
    the wiring in ``AgentLoop`` is one-to-one.
    """

    def __init__(
        self,
        dispatch_fn: DispatchFn,
        reply_target: DeliveryTarget,
        *,
        flush_chars: int = 200,
        flush_interval_ms: int = 1500,
        min_chars: int = 80,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        if dispatch_fn is None or reply_target is None:
            raise ValueError(
                "StreamFlusher requires both dispatch_fn and reply_target;"
                " caller should skip flusher construction otherwise"
            )
        self._dispatch_fn = dispatch_fn
        self._reply_target = reply_target
        # Clamp the knobs to sensible floors so a misconfigured operator
        # cannot accidentally turn streaming into a per-token fan-out.
        self._flush_chars = max(20, int(flush_chars))
        self._flush_interval = max(0.05, float(flush_interval_ms) / 1000.0)
        self._min_chars = max(1, int(min_chars))
        self._time_fn = time_fn

        self._buffer: str = ""
        self._aggregate_parts: list[str] = []
        self._last_flush_at: float = 0.0
        # ``_started_at`` is set on the first chunk seen; used to gate
        # time-based flushes (we never time-flush a brand-new buffer
        # that hasn't waited a full interval yet).
        self._started_at: float = 0.0
        self._has_dispatched: bool = False
        self._stats = StreamFlushStats()
        self._closed = False

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    @property
    def dispatched(self) -> bool:
        """Has at least one partial send actually been pushed?"""
        return self._has_dispatched

    @property
    def stats(self) -> StreamFlushStats:
        """Snapshot of flush telemetry. Mutated by the flusher."""
        return self._stats

    def aggregate_text(self) -> str:
        """Full visible text accumulated so far.

        Includes any unflushed buffer — caller can write this to the
        wiki cache + memory regardless of how many partial sends went
        out.
        """
        return "".join(self._aggregate_parts)

    async def on_chunk(self, text: str) -> None:
        """Receive one ``content`` delta from the LLM stream.

        Called by ``LLMClient`` via the ``stream_callback`` contract.
        Empty / whitespace-only deltas are still accumulated (they may
        carry meaningful punctuation / spacing) but never trigger a
        flush on their own.
        """
        if self._closed:
            self._stats.suppressed_count += 1
            return
        if not text:
            return
        self._buffer += text
        self._aggregate_parts.append(text)
        self._stats.aggregate_chars = sum(len(p) for p in self._aggregate_parts)
        now = self._time_fn()
        if self._started_at == 0.0:
            self._started_at = now
            self._last_flush_at = now
        if self._should_flush(now):
            await self._flush(now)

    async def finalize(self) -> StreamFlushStats:
        """Drain any remaining buffered text and close the flusher.

        Subsequent calls are idempotent. Returns the populated stats
        so the caller can log a single line.
        """
        if self._stats.finalized:
            return self._stats
        if self._buffer.strip():
            try:
                await self._flush(self._time_fn(), final=True)
                self._stats.finalize_caused_flush = True
            except Exception as exc:  # noqa: BLE001 - finalize must never raise
                logger.warning("[stream] finalize flush failed: {}", exc)
        self._closed = True
        self._stats.finalized = True
        return self._stats

    async def cancel(self) -> StreamFlushStats:
        """Best-effort drain on the error path.

        Behaves like :meth:`finalize`; provided as a separate name so
        callers can express intent (``except: await flusher.cancel()``).
        """
        return await self.finalize()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _should_flush(self, now: float) -> bool:
        # Char-based trigger fires immediately once the buffer is big
        # enough — this is the dominant path on snappy networks.
        if len(self._buffer) >= self._flush_chars:
            return True
        # Time-based trigger only fires after we have at least the
        # minimum visible chunk *and* enough time has passed since the
        # last send (or stream start). Keeps the very first send from
        # firing on a single character.
        if (
            len(self._buffer) >= self._min_chars
            and (now - self._last_flush_at) >= self._flush_interval
        ):
            return True
        return False

    async def _flush(self, now: float, *, final: bool = False) -> None:
        text_to_send = self._buffer
        if not text_to_send:
            return
        # Strip a leading newline only on the very first send so the IM
        # message doesn't open with awkward whitespace; subsequent
        # sends preserve newlines verbatim because they may be the only
        # thing separating two adjacent chunks.
        if not self._has_dispatched:
            text_to_send = text_to_send.lstrip()
            if not text_to_send:
                # All-whitespace first chunk — wait for real content.
                return
        self._buffer = ""
        try:
            await self._dispatch_fn(
                OutgoingMessage(
                    target=self._reply_target,
                    text=text_to_send,
                    meta={
                        "stream": True,
                        "stream_final": final,
                    },
                )
            )
        except Exception as exc:  # noqa: BLE001 - never fail the LLM on a flaky IM
            logger.warning(
                "[stream] partial flush failed (chars={}, final={}): {}",
                len(text_to_send), final, exc,
            )
            # We DON'T re-raise; the surrounding LLM call must keep
            # running so the user still gets a final answer that
            # AgentLoop can dispatch via the normal aggregate path.
            return
        self._has_dispatched = True
        self._stats.flush_count += 1
        self._last_flush_at = now
