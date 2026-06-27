"""Static security scanner for skill_manage writes.

Three verdicts, ranked from softest to hardest:

* ``safe``       — no findings, write proceeds.
* ``caution``    — at least one HIGH-severity finding (e.g. a reference
                   to ``~/.ssh`` keys, password-shaped strings). The
                   foreground agent surfaces a warning and proceeds;
                   the background-review fork is held to a tighter
                   bar and is blocked unless strict_for_agent=False.
* ``dangerous``  — at least one CRITICAL finding (prompt injection,
                   secret exfiltration, destructive shell, reverse
                   shell). Always blocked.

Patterns are deliberately narrow — false positives stop legitimate
operator-driven skill creation and must be tuned conservatively. We
prefer to miss a sneaky variant rather than reject a benign skill.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Optional

from ..core.provenance import BACKGROUND_REVIEW, FOREGROUND


VERDICT_SAFE = "safe"
VERDICT_CAUTION = "caution"
VERDICT_DANGEROUS = "dangerous"


class Severity(str, Enum):
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(slots=True)
class Finding:
    pattern_id: str
    category: str  # injection | destructive | exfiltration | reverse_shell | secret_dir
    severity: Severity
    snippet: str = ""

    def to_dict(self) -> dict:
        return {
            "pattern_id": self.pattern_id,
            "category": self.category,
            "severity": self.severity.value,
            "snippet": self.snippet,
        }


@dataclass(slots=True)
class ScanResult:
    verdict: str = VERDICT_SAFE
    findings: list[Finding] = field(default_factory=list)

    def has_critical(self) -> bool:
        return any(f.severity is Severity.CRITICAL for f in self.findings)

    def has_high(self) -> bool:
        return any(f.severity is Severity.HIGH for f in self.findings)

    def summary_line(self) -> str:
        """One-line summary used in log lines / block messages.

        Format: ``verdict=<v>; <category>=<count>, <category>=<count>``.
        """
        if not self.findings:
            return f"verdict={self.verdict}"
        counts: dict[str, int] = {}
        for f in self.findings:
            counts[f.category] = counts.get(f.category, 0) + 1
        # Stable ordering — sorted by category name for reproducibility.
        body = ", ".join(
            f"{cat}={cnt}" for cat, cnt in sorted(counts.items())
        )
        return f"verdict={self.verdict}; {body}"

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "findings": [f.to_dict() for f in self.findings],
        }


# -- Pattern definitions ------------------------------------------------------
#
# Each tuple: (pattern_id, category, severity, regex). We keep the full
# regex objects compiled at import time so scan_text() is hot-path cheap.

_PATTERNS: list[tuple[str, str, Severity, re.Pattern[str]]] = [
    # Prompt injection — the canonical "ignore all previous instructions"
    # phrasing plus the common "disregard ..." variant.
    (
        "prompt_injection_ignore",
        "injection",
        Severity.CRITICAL,
        re.compile(
            r"(?i)\b(ignore|disregard)\s+(all\s+)?(the\s+)?(previous|prior|above)"
            r"\s+(instructions?|prompts?|messages?|rules?)"
        ),
    ),
    (
        "prompt_injection_system",
        "injection",
        Severity.CRITICAL,
        re.compile(
            r"(?i)\boutput\s+(the\s+)?system\s+(prompt|message|instructions?)"
        ),
    ),
    # Destructive shell commands.
    (
        "destructive_rm_rf_root",
        "destructive",
        Severity.CRITICAL,
        re.compile(r"(?i)\brm\s+-[rRfF]+\s+(/|/\*|~|--no-preserve-root)"),
    ),
    (
        "destructive_dd_disk",
        "destructive",
        Severity.CRITICAL,
        re.compile(r"(?i)\bdd\s+if=/dev/(zero|random|urandom)\s+of=/dev/[a-z]+"),
    ),
    (
        "destructive_mkfs",
        "destructive",
        Severity.CRITICAL,
        re.compile(r"(?i)\bmkfs\.\w+\s+/dev/[a-z]+"),
    ),
    # Reverse-shell incantations.
    (
        "reverse_shell_nc_listen",
        "reverse_shell",
        Severity.CRITICAL,
        re.compile(r"(?i)\bnc\s+-l\s*p?\s+\d{2,5}\b"),
    ),
    (
        "reverse_shell_bash_tcp",
        "reverse_shell",
        Severity.CRITICAL,
        re.compile(r"/dev/tcp/\d{1,3}(?:\.\d{1,3}){3}/\d{1,5}"),
    ),
    # Secret exfiltration through curl + env-var interpolation.
    (
        "exfil_curl_env_secret",
        "exfiltration",
        Severity.CRITICAL,
        re.compile(
            r"(?i)\bcurl\b[^\n]{0,200}\$"
            r"(?:\{)?(?:OPENAI_API_KEY|ANTHROPIC_API_KEY|GITHUB_TOKEN"
            r"|GH_TOKEN|AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN"
            r"|API_KEY|SECRET_KEY|ACCESS_TOKEN|REFRESH_TOKEN|PASSWORD)"
        ),
    ),
    (
        "exfil_wget_env_secret",
        "exfiltration",
        Severity.CRITICAL,
        re.compile(
            r"(?i)\bwget\b[^\n]{0,200}\$"
            r"(?:\{)?(?:OPENAI_API_KEY|ANTHROPIC_API_KEY|GITHUB_TOKEN"
            r"|GH_TOKEN|AWS_SECRET_ACCESS_KEY|API_KEY|SECRET_KEY|ACCESS_TOKEN)"
        ),
    ),
    # HIGH-severity hints that *might* be a problem in context — surfaced
    # as caution so the operator sees them but the foreground write
    # proceeds.
    (
        "secret_dir_ssh",
        "exfiltration",
        Severity.HIGH,
        re.compile(r"(?<![\w/])~/\.ssh(?![\w/])"),
    ),
    (
        "secret_dir_aws",
        "exfiltration",
        Severity.HIGH,
        re.compile(r"(?<![\w/])~/\.aws/credentials(?![\w])"),
    ),
    (
        "secret_dir_netrc",
        "exfiltration",
        Severity.HIGH,
        re.compile(r"(?<![\w/])~/\.netrc(?![\w])"),
    ),
]


def scan_text(text: Optional[str]) -> ScanResult:
    """Run every pattern against ``text`` and return the worst verdict."""
    if not text or not isinstance(text, str):
        return ScanResult(verdict=VERDICT_SAFE)
    findings: list[Finding] = []
    for pattern_id, category, severity, pattern in _PATTERNS:
        try:
            m = pattern.search(text)
        except re.error:
            # A pathological pattern shouldn't take the whole guard
            # offline; skip it and keep scanning.
            continue
        if m is not None:
            snippet = (m.group(0) or "")[:80]
            findings.append(
                Finding(
                    pattern_id=pattern_id,
                    category=category,
                    severity=severity,
                    snippet=snippet,
                )
            )
    verdict = VERDICT_SAFE
    if any(f.severity is Severity.CRITICAL for f in findings):
        verdict = VERDICT_DANGEROUS
    elif findings:
        verdict = VERDICT_CAUTION
    return ScanResult(verdict=verdict, findings=findings)


class SkillGuard:
    """Stateful policy wrapper around :func:`scan_text`.

    Two knobs:

    * ``enabled`` — master kill-switch. ``False`` makes every call a
      no-op (useful for smoke tests and break-glass).
    * ``strict_for_agent`` — when True (default), background-review
      writes are blocked on ``caution`` verdicts too, not just
      ``dangerous`` ones. Foreground writes always allow caution.
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        strict_for_agent: bool = True,
    ) -> None:
        self.enabled = bool(enabled)
        self.strict_for_agent = bool(strict_for_agent)

    def scan(self, text: Optional[str]) -> ScanResult:
        if not self.enabled:
            return ScanResult(verdict=VERDICT_SAFE)
        return scan_text(text)

    def should_block(
        self,
        result: ScanResult,
        *,
        origin: str = FOREGROUND,
    ) -> bool:
        if not self.enabled:
            return False
        if result.verdict == VERDICT_DANGEROUS:
            return True
        if result.verdict == VERDICT_CAUTION:
            # Strict-for-agent: the silent review fork can't see the
            # block itself, so we hold its writes to a tighter bar.
            if self.strict_for_agent and origin == BACKGROUND_REVIEW:
                return True
        return False

    def block_message(self, result: ScanResult) -> str:
        """Single-line explanation for the refusal ToolResult."""
        if not result.findings:
            return "SkillGuard refused write (no detail)."
        head = f"SkillGuard refused write ({result.summary_line()})."
        # Surface the first critical pattern_id (or first finding) so the
        # LLM can self-correct.
        primary = next(
            (f for f in result.findings if f.severity is Severity.CRITICAL),
            result.findings[0],
        )
        return f"{head} flagged={primary.pattern_id}"


__all__ = [
    "VERDICT_SAFE",
    "VERDICT_CAUTION",
    "VERDICT_DANGEROUS",
    "Severity",
    "Finding",
    "ScanResult",
    "SkillGuard",
    "scan_text",
]
