"""Static guard for skill text."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class SkillScanVerdict(str, Enum):
    SAFE = "safe"
    CAUTION = "caution"
    DANGEROUS = "dangerous"


class SkillFindingSeverity(str, Enum):
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class SkillFinding:
    pattern_id: str
    category: str
    severity: SkillFindingSeverity
    snippet: str = ""


@dataclass(frozen=True, slots=True)
class SkillScanResult:
    verdict: SkillScanVerdict = SkillScanVerdict.SAFE
    findings: tuple[SkillFinding, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "findings", tuple(self.findings))

    def has_critical(self) -> bool:
        return any(item.severity is SkillFindingSeverity.CRITICAL for item in self.findings)


_PATTERNS: tuple[tuple[str, str, SkillFindingSeverity, re.Pattern[str]], ...] = (
    (
        "prompt_injection_ignore",
        "injection",
        SkillFindingSeverity.CRITICAL,
        re.compile(
            r"(?i)\b(ignore|disregard)\s+(all\s+)?(previous|prior|above)"
            r"\s+(instructions?|prompts?|rules?)"
        ),
    ),
    (
        "prompt_exfil_system",
        "exfiltration",
        SkillFindingSeverity.CRITICAL,
        re.compile(r"(?i)\b(output|print|reveal)\s+(the\s+)?system\s+prompt"),
    ),
    (
        "destructive_rm_rf",
        "destructive",
        SkillFindingSeverity.CRITICAL,
        re.compile(r"(?i)\brm\s+-[rRfF]+\s+(/|/\*|~|--no-preserve-root)"),
    ),
    (
        "reverse_shell_tcp",
        "reverse_shell",
        SkillFindingSeverity.CRITICAL,
        re.compile(r"/dev/tcp/\d{1,3}(?:\.\d{1,3}){3}/\d{1,5}"),
    ),
    (
        "secret_dir_ssh",
        "secret_dir",
        SkillFindingSeverity.HIGH,
        re.compile(r"(?<![\w/])~/\.ssh(?![\w/])"),
    ),
)


def scan_skill_text(text: str | None) -> SkillScanResult:
    """Return static scan result for skill text."""

    if not text:
        return SkillScanResult()
    findings: list[SkillFinding] = []
    for pattern_id, category, severity, pattern in _PATTERNS:
        match = pattern.search(text)
        if match is None:
            continue
        findings.append(
            SkillFinding(
                pattern_id=pattern_id,
                category=category,
                severity=severity,
                snippet=match.group(0)[:80],
            )
        )
    if any(item.severity is SkillFindingSeverity.CRITICAL for item in findings):
        verdict = SkillScanVerdict.DANGEROUS
    elif findings:
        verdict = SkillScanVerdict.CAUTION
    else:
        verdict = SkillScanVerdict.SAFE
    return SkillScanResult(verdict=verdict, findings=tuple(findings))


class SkillGuard:
    """Policy wrapper around static skill scan."""

    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled

    def scan(self, text: str | None) -> SkillScanResult:
        if not self.enabled:
            return SkillScanResult()
        return scan_skill_text(text)

    def should_block(self, result: SkillScanResult) -> bool:
        return self.enabled and result.verdict is SkillScanVerdict.DANGEROUS
