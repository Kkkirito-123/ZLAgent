"""User-correction detector (v0.15).

When the user says something like *"不对"* / *"应该是"* / *"actually"* /
*"I meant"*, that single message is one of the most valuable training
signals the agent ever sees — Hermes' SKILL_REVIEW_PROMPT explicitly
calls it out as a top-priority trigger for writing an ``agent_note``.

The v0.13 review prompt mentions this, but relies on the LLM to recognise
the pattern from a noisy main-turn summary, which is unreliable for short
exchanges. v0.15 adds a deterministic detector:

* Bilingual regex (Chinese + English) over the user's incoming text
* Confidence levels (low/medium/high) so we can scale how strongly the
  review fork is told to act
* Stateless — pure function over a single string

The detector NEVER decides what to remember; it only flags the moment
"this turn might be a correction worth recording" and the review fork
takes over from there.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class CorrectionConfidence(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(slots=True, frozen=True)
class CorrectionSignal:
    """A normalised description of a detected correction."""

    confidence: CorrectionConfidence
    phrase: str  # the trigger phrase that fired (for logging / review hint)


# (regex, confidence) pairs. Order matters — higher-confidence patterns
# come first so the first match wins.
#
# Confidence rationale:
#   HIGH    — explicit "you were wrong" / "actually" / "我意思是" — almost
#             always a teaching moment.
#   MEDIUM  — "应该" / "不是" / "should" — softer corrections; could be a
#             user's *first* statement of intent rather than a fix.
#   LOW     — bare negations like "no" / "不"; only useful if other turns
#             confirm the pattern, which we leave to the LLM.
_CORRECTION_PATTERNS: list[tuple[str, CorrectionConfidence]] = [
    # ── Chinese, high confidence ─────────────────────────────────────────
    (r"不对[，,。！!\s]?", CorrectionConfidence.HIGH),
    (r"错了[，,。！!\s]?", CorrectionConfidence.HIGH),
    (r"我意思是", CorrectionConfidence.HIGH),
    (r"我(?:刚才)?说的是", CorrectionConfidence.HIGH),
    (r"不是这样", CorrectionConfidence.HIGH),
    (r"重新(?:理解|做|来)", CorrectionConfidence.HIGH),
    # ── English, high confidence ─────────────────────────────────────────
    (r"\bthat'?s\s+wrong\b", CorrectionConfidence.HIGH),
    (r"\byou'?re\s+wrong\b", CorrectionConfidence.HIGH),
    (r"\bactually[,\s]", CorrectionConfidence.HIGH),
    (r"\bI\s+meant\b", CorrectionConfidence.HIGH),
    (r"\blet\s+me\s+(?:clarify|rephrase|correct)\b", CorrectionConfidence.HIGH),
    (r"\bcorrection[:\s]", CorrectionConfidence.HIGH),
    # ── Chinese, medium confidence ───────────────────────────────────────
    (r"不是[，,。！!\s]?(?:这|那|我|你)", CorrectionConfidence.MEDIUM),
    (r"应该是", CorrectionConfidence.MEDIUM),
    (r"应该用", CorrectionConfidence.MEDIUM),
    (r"不要(?:用|这样)", CorrectionConfidence.MEDIUM),
    # ── English, medium confidence ───────────────────────────────────────
    (r"\bshould\s+be\b", CorrectionConfidence.MEDIUM),
    (r"\bnot\s+(?:that|like|the)\b", CorrectionConfidence.MEDIUM),
    (r"\bno[,\s]+it'?s\b", CorrectionConfidence.MEDIUM),
]


_COMPILED: list[tuple[re.Pattern[str], CorrectionConfidence]] = [
    (re.compile(pat, re.IGNORECASE), conf) for pat, conf in _CORRECTION_PATTERNS
]


def detect_correction(text: str) -> Optional[CorrectionSignal]:
    """Return a :class:`CorrectionSignal` if the text looks like a correction.

    Returns ``None`` when no pattern matches. The first match wins, so
    high-confidence patterns are checked first.
    """
    if not text or not isinstance(text, str):
        return None
    body = text.strip()
    if not body:
        return None
    # Soft length cap — a 5000-char paragraph is unlikely to be a brief
    # correction; treat as no-signal.
    if len(body) > 800:
        return None
    for pattern, confidence in _COMPILED:
        match = pattern.search(body)
        if match:
            return CorrectionSignal(
                confidence=confidence,
                phrase=match.group(0).strip(),
            )
    return None


def render_review_hint(signal: CorrectionSignal) -> str:
    """Produce the snippet to splice into the review fork's user prompt."""
    return (
        f"\n\n## ⚠️ 用户纠正信号 (confidence={signal.confidence.value})"
        f"\n用户在本轮里使用了 “{signal.phrase}” 这个表达，这通常意味着 ta 在"
        "**纠正你之前的某个理解或行为**。这种瞬间是最重要的"
        " agent_note 写入触发条件之一（review 系统提示中第 2 条）。"
        "\n请仔细阅读上面的对话，判断用户具体在纠正什么"
        "——是事实性更正（“它叫 X 不叫 Y”）还是偏好更正（“以后别这样”）"
        "——并：\n"
        "1. 如果是**长期适用**的事实/偏好 → `memory_manage(remember, "
        "kind=user_fact, ...)` 写一条简洁条目。\n"
        "2. 如果是**关于你工作方式**的纠正（比如某个 skill 的 pitfall）→"
        " `skill_manage(action='patch', ...)` 把这个 pitfall 加到对应"
        " SKILL.md 的 Pitfalls 段。\n"
        "3. 两者都不像 → 至少写一条 `kind=agent_note` 描述这次纠正的语境，"
        "供未来同类场景参考。"
    )
