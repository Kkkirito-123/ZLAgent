"""Strip hallucinated XML/DSML tool-call attempts from LLM content.

Some providers (DeepSeek V4 Pro observed in 2026-05) emit tool-call
intents as XML or DSML-style text inside the ``content`` field
instead of using the structured ``tool_calls`` field required by the
OpenAI Chat Completions protocol. ZLAgent's streaming defence only
watches ``delta.tool_calls``, so those hallucinated XML blocks leak
straight into the user-facing IM message and look like garbage:

    搜索暂时不可用。让我尝试通过学术搜索引擎直接获取论文。

    < | | DSML | | tool_calls> < | | DSML | | invoke name="read_url">
    < | | DSML | | parameter name="url" string="true">https://...

This module provides two pure-function primitives the LLM client
uses to defuse that failure mode without depending on any AgentLoop
state:

* :func:`contains_hallucinated_tool_call` — cheap substring test.
  Used by the streaming dispatcher to STOP forwarding ``content``
  deltas to the IM gateway the instant the buffered text crosses
  into XML hallucination. The user keeps whatever legitimate
  narration was flushed before the XML began; nothing new appears.

* :func:`strip_hallucinated_tool_calls` — regex-based block removal.
  Applied to the final aggregated ``content`` before it leaves the
  LLM client, so the AgentLoop sees a clean assistant message even
  when the wire response was contaminated. The function preserves
  legitimate prose interleaved between XML blocks (some providers
  alternate narration + fake tool call + narration; we want to keep
  rounds 1 and 3 and drop round 2).

False-positive risk is low: the token ``DSML`` is unique enough that
it is not expected in legitimate user-facing text. The XML pattern
requires multiple structural tokens to co-occur, which makes a stray
``<parameter`` inside a Markdown code block safe.
"""
from __future__ import annotations

import re
from typing import Final

# the cheap stream-time guard. We deliberately use a plain
# substring test on the very specific token ``DSML``: it is small,
# vendor-neutral, and the model's hallucination uses it consistently
# as a wrapper marker even when the inner tag names vary. Catching it
# on the FIRST chunk that contains it stops the IM gateway from
# pushing any further visible text to the user.
_STREAM_TRIPWIRE: Final[tuple[str, ...]] = ("DSML", "<function_calls>")


def contains_hallucinated_tool_call(text: str) -> bool:
    """Return True when ``text`` shows the signs of an XML/DSML tool-call
    hallucination. Designed to be O(n) and cheap enough to call once
    per streaming chunk.
    """
    if not text:
        return False
    for marker in _STREAM_TRIPWIRE:
        if marker in text:
            return True
    return False


# the post-aggregation scrubber. The two patterns cover the
# observed failure modes:
#
# * ``DSML``-style: ``< | | DSML | | tool_calls> ... < | | DSML | |
#   tool_calls>`` where the outer ``DSML`` token brackets one or more
#   pipe-separated XML-ish tags. Captures everything from the opening
#   ``DSML`` marker through to the closing one (greedy on lines but
#   non-greedy across them so two separate blocks don't collapse).
#
# * ``<function_calls>``-style: Claude Sonnet's documented XML
#   format. Captured as a balanced tag for symmetry with the
#   ``DSML`` case. Some hallucinations forget the closing tag, so we
#   also accept end-of-string as a terminator.
# v0.45.2 — DeepSeek V4 Pro and a few of its peers also emit
# **fullwidth-pipe** variants of the DSML opener: ``<｜｜DSML｜｜>``
# (U+FF5C). The ASCII-only ``\|`` in the previous regex left those
# blocks untouched, so even after the streaming scrubber latched
# (which it does, since ``DSML`` matches as a substring) the post-
# aggregation aggregate still contained the full DSML block and got
# dispatched to IM. The character class ``[|｜]`` matches both pipe
# variants without affecting the legitimate-text false-positive risk
# (neither pipe form is common in user-facing prose).
_DSML_BLOCK_RE: Final[re.Pattern[str]] = re.compile(
    r"<\s*[|｜].*?DSML.*?[|｜]\s*tool_calls\s*>.*?"
    r"(?:<\s*/?\s*[|｜].*?DSML.*?[|｜]\s*tool_calls\s*>|\Z)",
    re.DOTALL | re.IGNORECASE,
)
_FUNCTION_CALLS_BLOCK_RE: Final[re.Pattern[str]] = re.compile(
    r"<\s*function_calls\s*>.*?(?:<\s*/\s*function_calls\s*>|\Z)",
    re.DOTALL | re.IGNORECASE,
)
# Tidy up the triple-blank-line craters left when a block is excised
# from the middle of multi-paragraph narration.
_BLANK_LINE_RUN_RE: Final[re.Pattern[str]] = re.compile(r"\n{3,}")


def strip_hallucinated_tool_calls(text: str) -> str:
    """Return ``text`` with hallucinated tool-call XML blocks removed.

    Idempotent and safe on text that contains none of the patterns:
    the function returns the input unchanged when neither tripwire
    fires, so it is fine to invoke on every assistant response.
    """
    if not text or not contains_hallucinated_tool_call(text):
        return text
    cleaned = _DSML_BLOCK_RE.sub("", text)
    cleaned = _FUNCTION_CALLS_BLOCK_RE.sub("", cleaned)
    cleaned = _BLANK_LINE_RUN_RE.sub("\n\n", cleaned)
    return cleaned.strip()


class StreamingHallucinationScrubber:
    """Stateful streaming scrubber that survives chunk boundaries.

    The one-shot :func:`contains_hallucinated_tool_call` substring test
    cannot defuse the leak when the tripwire token (``DSML`` or
    ``<function_calls>``) is split across deltas. Real example from
    DeepSeek V4 Pro logs:

        chunk 1: ``< | | ``
        chunk 2: ``DSML | |``
        chunk 3: `` tool_calls>``

    The one-shot detector only fires on chunk 3; chunks 1 and 2 already
    flushed to the IM gateway, so the user sees `< | | DSML | |` flicker
    in before the rest gets suppressed.

    This scrubber keeps a small **suspicion tail** that could be the
    start of a tripwire. Once we're certain the tail is NOT the start
    of a tripwire (e.g. the next char makes the prefix impossible), it
    flushes out. Once it IS confirmed a tripwire, the scrubber
    permanently latches and drops everything afterwards.

    Usage::

        scrubber = StreamingHallucinationScrubber()
        for delta in stream:
            visible = scrubber.feed(delta)
            if visible:
                emit(visible)
        # End-of-stream: any tail that's safely non-tripwire flushes.
        tail = scrubber.flush()
        if tail:
            emit(tail)

    The scrubber is single-use per turn. Construct a fresh one for
    each new streaming response.
    """

    # The full tripwire markers that fully match a hallucination. Any
    # prefix of these strings is also "suspicious" — we hold it back.
    # ``<memory-context`` is added defensively in v0.45: if the LLM
    # ever echoes the fence back into user-facing content, we suppress
    # before the user sees system-prompt internals.
    #
    # v0.45.1 — added ``<|DSML`` and ``< | | DSML`` literally because
    # the bare ``DSML`` token only fires AFTER the leading XML/pipe
    # prefix has already streamed through the prefix-hold check (which
    # only holds suffixes that match a *tripwire* prefix; ``<``, ``< ``,
    # ``< |`` weren't holds because no tripwire started that way). The
    # 2026-05-12 user report ("这些代码都是为什么") was the symptom: the
    # 5-char prefix ``< | | `` reached the IM before ``DSML`` arrived in
    # the next chunk and latched. With these added the longest-prefix
    # path holds back any incipient DSML opener regardless of variant.
    #
    # v0.45.2 — added the **fullwidth-pipe** variants ``<｜DSML`` and
    # ``<｜｜DSML``. The 2026-05-12 zlagent.log showed the user's reply
    # quoting the actual leaked text containing U+FF5C ('｜'), not
    # ASCII '|'. Without these the ``DSML`` substring still latches
    # (so latching IS triggered as the warning log proves), but the
    # leftmost-tripwire visible cut leaves ``<｜｜`` as the prefix to
    # IM. The retroactive-opener trim below also helps for arbitrary
    # future variants we haven't enumerated.
    _TRIPWIRES: Final[tuple[str, ...]] = (
        "DSML",
        "<function_calls>",
        "<memory-context",
        "<|DSML",
        "< | | DSML",
        "<｜DSML",
        "<｜｜DSML",
    )
    # Structural characters that may legitimately appear between an
    # opening ``<`` and the ``DSML`` keyword in a hallucinated opener.
    # Used by the retroactive-opener trim and the generalized opener-
    # hold scan to drop arbitrary spacing / pipe / fullwidth-pipe /
    # slash patterns without enumerating every literal variant in
    # ``_TRIPWIRES``.
    _OPENER_STRUCTURAL_CHARS: Final[frozenset[str]] = frozenset(
        " \t|｜/\\"
    )
    # v0.45.2 — chars allowed inside an in-progress opener tag from
    # ``<`` up to (but not including) the closing ``>``. Superset of
    # structural chars plus the ASCII letters / ``_`` / ``-`` that
    # appear in legitimate hallucinated keywords (DSML, function_calls,
    # memory-context). Excluding digits + Chinese punctuation means
    # text like ``"x < 5"`` does NOT get held back — the digit ``5``
    # disqualifies the candidate during the backward scan.
    _OPENER_TAIL_CHARS: Final[frozenset[str]] = frozenset(
        " \t|｜/\\"
        "abcdefghijklmnopqrstuvwxyz"
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        "_-"
    )
    # Maximum span we look back from the latch point to a recent ``<``
    # when retroactively trimming a partial opener. 24 chars is more
    # than enough for ``< | | DSML``, ``<｜｜DSML``, and any near-
    # neighbour we've seen; large enough leaks would have triggered
    # the per-tripwire ``find()`` on the fast path anyway.
    _OPENER_LOOKBACK: Final[int] = 24
    # Maximum length of any tripwire — the suspicion window is at most
    # this many chars.
    _MAX_TRIPWIRE_LEN: Final[int] = max(len(t) for t in _TRIPWIRES)

    def __init__(self) -> None:
        self._latched: bool = False
        self._tail: str = ""

    @property
    def latched(self) -> bool:
        """True iff the scrubber has confirmed a hallucination and is
        permanently suppressing further output."""
        return self._latched

    def feed(self, text: str) -> str:
        """Consume ``text`` from the wire stream; return the safely
        emittable portion. Suspicious trailing fragments are held in
        an internal buffer and re-checked on the next feed()."""
        if self._latched:
            return ""
        if not text:
            return ""
        buf = self._tail + text
        self._tail = ""

        # Fast path: full tripwire already in buffer — latch + drop
        # everything from the LEFTMOST tripwire onward.
        #
        # v0.45.1 — must scan ALL tripwires and pick the earliest hit,
        # not just the first one that matches. The previous loop returned
        # on first match, which meant a short tripwire (``DSML`` at idx
        # 6 of ``< | | DSML | | tool_calls>``) won over a more-specific
        # longer tripwire (``< | | DSML`` at idx 0) and emitted the
        # ``< | | `` prefix to IM before latching. Picking the earliest
        # idx ensures the entire opener is suppressed.
        earliest_idx = -1
        for marker in self._TRIPWIRES:
            idx = buf.find(marker)
            if idx != -1 and (earliest_idx == -1 or idx < earliest_idx):
                earliest_idx = idx
        if earliest_idx != -1:
            visible = buf[:earliest_idx]
            # v0.45.2 — retroactive opener trim. Even with the
            # leftmost-idx fix, a NEW pipe variant we haven't
            # enumerated would re-leak the ``<...`` opener bytes that
            # already arrived in earlier chunks (held in tail) or in
            # ``buf[:earliest_idx]``. Walk backward from the latch
            # point until either we find a recent ``<`` whose tail
            # is structural-only (spaces/pipes/fullwidth-pipes/slashes)
            # or we exceed ``_OPENER_LOOKBACK``. Drop everything from
            # that ``<`` onward. False-positive risk: the prose
            # ``"Use < and > carefully"`` followed by ``DSML`` would
            # lose the ``<``; vanishingly rare and worth the safety.
            if visible:
                cut = self._retroactive_opener_cut(visible)
                if cut < len(visible):
                    visible = visible[:cut]
            self._latched = True
            return visible

        # Slow path: no full tripwire yet, but maybe a prefix. The tail
        # we need to hold back is the longest suffix of ``buf`` that
        # equals a prefix of any tripwire. Cap at MAX_TRIPWIRE_LEN-1
        # because a full tripwire would already have matched above.
        hold = self._longest_tripwire_prefix(buf)
        if hold == 0:
            return buf
        self._tail = buf[-hold:]
        return buf[:-hold]

    def flush(self) -> str:
        """End-of-stream: surface any tail that was safely non-tripwire."""
        if self._latched:
            self._tail = ""
            return ""
        tail = self._tail
        self._tail = ""
        return tail

    @classmethod
    def _retroactive_opener_cut(cls, visible: str) -> int:
        """Find the index in ``visible`` where a trailing ``<...``
        partial opener begins. Returns ``len(visible)`` when the tail
        looks like normal prose (no trim needed).

        A "partial opener" is the literal char ``<`` followed by 0..N
        :data:`_OPENER_STRUCTURAL_CHARS` (spaces, pipes both width,
        slashes). Any other character — including alphabetic letters,
        Chinese punctuation, digits — disqualifies the candidate ``<``
        and we keep walking left until the lookback budget runs out.
        Returning the smallest qualifying index is correct because the
        trailing structural fragment is exactly what we want to drop.
        """
        n = len(visible)
        if n == 0:
            return 0
        # Anchor scan to the rightmost ``<`` within the lookback window
        # whose suffix is structural-only.
        start = max(0, n - cls._OPENER_LOOKBACK)
        i = n - 1
        cut: int = n  # default: no trim
        while i >= start:
            ch = visible[i]
            if ch == "<":
                # Verify everything from i+1 to n is structural-only.
                suffix = visible[i + 1 : n]
                if all(c in cls._OPENER_STRUCTURAL_CHARS for c in suffix):
                    cut = i
                # Either way (matched or not), stop — the opener we
                # want is the *closest* ``<`` to the latch point;
                # earlier ``<`` chars belong to legitimate prose.
                break
            if ch not in cls._OPENER_STRUCTURAL_CHARS:
                # Hit a content char before any ``<``. The latched
                # tripwire is preceded by real prose, so visible is
                # safe to keep verbatim.
                break
            i -= 1
        return cut

    @classmethod
    def _longest_tripwire_prefix(cls, buf: str) -> int:
        """Return the length of the longest suffix of ``buf`` to hold
        back as a potentially-unfinished hallucination opener. ``0``
        means the buffer is entirely safe to flush.

        Two checks combined; we keep the LONGER hold:

        * **Enumerated tripwire prefix.** Suffix equals a prefix of
          any specific marker in :data:`_TRIPWIRES`. Catches the
          known DSML / function_calls / memory-context openers.

        * **Generalized structural opener** (v0.45.2). Suffix begins
          with ``<`` and contains only :data:`_OPENER_STRUCTURAL_CHARS`
          afterwards. Holds back arbitrary new pipe variants we
          haven't enumerated (e.g. ``< || `` with no spacing) so they
          can be examined in the next feed where ``DSML`` will arrive
          and the fast-path latch + retroactive trim fire together.
        """
        n = len(buf)
        if n == 0:
            return 0
        # 1. Tripwire-prefix scan (legacy logic).
        scan_len = min(n, cls._MAX_TRIPWIRE_LEN - 1)
        tripwire_hold = 0
        for k in range(scan_len, 0, -1):
            suffix = buf[-k:]
            for marker in cls._TRIPWIRES:
                if marker.startswith(suffix):
                    tripwire_hold = k
                    break
            if tripwire_hold:
                break

        # 2. Generalized opener scan. Walk back from the last char
        # accepting the wider :data:`_OPENER_TAIL_CHARS` set (structural
        # + ASCII letters / underscore / hyphen) so we can hold an
        # in-progress hallucinated opener whose keyword has only
        # arrived partially (e.g. ``< || D`` where the next chunk will
        # complete it to ``< || DSML``). Stop the scan at:
        #   * a ``<`` — candidate opener; if no ``>`` appears in its
        #     suffix, hold from this ``<`` onward (any in-flight tag);
        #   * any other char — disqualifies the tail (real prose
        #     encountered, no unfinished tag at the boundary).
        opener_hold = 0
        look_start = max(0, n - cls._OPENER_LOOKBACK)
        i = n - 1
        while i >= look_start:
            ch = buf[i]
            if ch == "<":
                tail = buf[i + 1 : n]
                # ``>`` in the tail means the tag is closed — not an
                # in-flight opener; let it flush.
                if ">" in tail:
                    break
                opener_hold = n - i
                break
            if ch not in cls._OPENER_TAIL_CHARS:
                break
            i -= 1

        return max(tripwire_hold, opener_hold)


__all__ = [
    "StreamingHallucinationScrubber",
    "contains_hallucinated_tool_call",
    "strip_hallucinated_tool_calls",
]
