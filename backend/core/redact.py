"""Secret redaction primitive.

Stateless regex-based scrubber for high-confidence secret tokens
(API keys, bearer tokens, KEY=value env exports). Used by
SummaryCompressor before handing chunks to the summarizer LLM and
by MCP credential rendering before logging.

Belongs to the core layer: no dependencies on agent/skills/tools.
"""
from __future__ import annotations

import re
from typing import Any, Pattern

REDACTED = "[REDACTED]"

_SENSITIVE_KEY = re.compile(
    r"(?i)(api[_-]?key|secret|token|password|passwd|authorization|cookie|credential)"
)

_PATTERNS: list[tuple[Pattern[str], str]] = [
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}\b"), REDACTED),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"), REDACTED),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), REDACTED),
    (re.compile(r"\bxox[abps]-[A-Za-z0-9-]{10,}\b"), REDACTED),
    (
        re.compile(r"(?i)\b(authorization\s*:\s*)(bearer|basic)\s+\S+"),
        r"\1\2 " + REDACTED,
    ),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|secret[_-]?key|access[_-]?token|"
            r"refresh[_-]?token|password|passwd|client[_-]?secret)"
            r"\s*[:=]\s*[\"']?([A-Za-z0-9._\-]{6,})[\"']?",
        ),
        r"\1=" + REDACTED,
    ),
]


def redact_text(text: str) -> str:
    """Return ``text`` with high-confidence secret tokens replaced.

    Empty / non-string input is returned unchanged. Failures inside any
    individual pattern are swallowed.
    """
    if not text or not isinstance(text, str):
        return text
    out = text
    for pat, repl in _PATTERNS:
        try:
            out = pat.sub(repl, out)
        except re.error:
            continue
    return out


def redact_mapping(value: Any, *, key: str = "") -> Any:
    """Recursively redact values whose mapping key names a credential."""
    if key and _SENSITIVE_KEY.search(key):
        return REDACTED
    if isinstance(value, dict):
        return {
            str(item_key): redact_mapping(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [redact_mapping(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_mapping(item) for item in value)
    if isinstance(value, str):
        return redact_text(value)
    return value


__all__ = ["REDACTED", "redact_mapping", "redact_text"]
