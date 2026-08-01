"""Bounded Skill discovery and progressive context loading."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from .guard import SkillScanVerdict
from .loader import FileSystemSkillLoader
from .types import SkillManifest


_WORD_RE = re.compile(r"[a-z0-9][a-z0-9._+-]*|[\u3400-\u9fff]+", re.IGNORECASE)
_LATIN_STOP_WORDS = {
    "a",
    "an",
    "and",
    "for",
    "from",
    "in",
    "of",
    "on",
    "skill",
    "the",
    "to",
    "use",
    "with",
}


@dataclass(frozen=True, slots=True)
class SkillSelection:
    """One installed Skill selected from metadata before its body is loaded."""

    manifest: SkillManifest
    score: int
    reasons: tuple[str, ...] = field(default_factory=tuple)
    body: str = field(default="", repr=False)
    body_digest: str = ""
    truncated: bool = False
    scan_verdict: SkillScanVerdict = SkillScanVerdict.SAFE

    def __post_init__(self) -> None:
        if self.score <= 0:
            raise ValueError("skill selection score must be positive")
        object.__setattr__(self, "reasons", tuple(self.reasons))

    def metadata_dict(self) -> dict[str, Any]:
        return {
            "id": self.manifest.id,
            "name": self.manifest.name,
            "version": self.manifest.version,
            "score": self.score,
            "reasons": list(self.reasons),
            "body_digest": self.body_digest,
            "truncated": self.truncated,
            "scan_verdict": self.scan_verdict.value,
        }

    def context_dict(self) -> dict[str, Any]:
        return {
            **self.metadata_dict(),
            "description": self.manifest.description,
            "instructions": self.body,
        }


class SkillSelector:
    """Select installed Skills from metadata and load only matched bodies."""

    def __init__(
        self,
        loader: FileSystemSkillLoader,
        *,
        max_selected: int = 2,
        max_body_chars: int = 3_000,
    ) -> None:
        if max_selected < 1:
            raise ValueError("max_selected must be >= 1")
        if max_body_chars < 256:
            raise ValueError("max_body_chars must be >= 256")
        self._loader = loader
        self._max_selected = max_selected
        self._max_body_chars = max_body_chars

    def select(self, query: str) -> tuple[SkillSelection, ...]:
        """Return safe, deterministic matches in descending score order."""

        normalized = query.casefold().strip()
        if not normalized:
            return ()
        query_terms = _terms(normalized)
        ranked: list[tuple[int, str, SkillManifest, tuple[str, ...]]] = []
        for manifest in self._loader.list():
            score, reasons = self._score(
                normalized,
                query_terms,
                manifest,
            )
            if score > 0:
                ranked.append((score, manifest.id, manifest, reasons))
        ranked.sort(key=lambda item: (-item[0], item[1]))

        selected: list[SkillSelection] = []
        for score, _, manifest, reasons in ranked:
            scan = self._loader.scan_body(manifest.id)
            if scan is not None and scan.verdict is SkillScanVerdict.DANGEROUS:
                continue
            body = self._loader.read_body(manifest.id) or ""
            bounded, truncated = _truncate(body, self._max_body_chars)
            if not bounded:
                continue
            selected.append(
                SkillSelection(
                    manifest=manifest,
                    score=score,
                    reasons=reasons,
                    body=bounded,
                    body_digest=(
                        "sha256:"
                        + hashlib.sha256(body.encode("utf-8")).hexdigest()
                    ),
                    truncated=truncated,
                    scan_verdict=(
                        scan.verdict if scan is not None else SkillScanVerdict.SAFE
                    ),
                )
            )
            if len(selected) >= self._max_selected:
                break
        return tuple(selected)

    def render_context(
        self,
        selections: tuple[SkillSelection, ...],
    ) -> str:
        """Render selected instructions as structured background data."""

        if not selections:
            return ""
        return json.dumps(
            [item.context_dict() for item in selections],
            ensure_ascii=False,
            sort_keys=True,
        )

    @staticmethod
    def _score(
        normalized_query: str,
        query_terms: set[str],
        manifest: SkillManifest,
    ) -> tuple[int, tuple[str, ...]]:
        score = 0
        reasons: list[str] = []
        for trigger in manifest.triggers:
            normalized_trigger = trigger.casefold().strip()
            if normalized_trigger and normalized_trigger in normalized_query:
                score += 100
                reasons.append(f"trigger:{trigger}")

        identities = {
            manifest.id.casefold().replace("-", " "),
            manifest.name.casefold(),
        }
        for identity in identities:
            if identity and identity in normalized_query:
                score += 40
                reasons.append(f"identity:{identity}")

        for tag in manifest.tags:
            normalized_tag = tag.casefold().strip()
            if normalized_tag and normalized_tag in normalized_query:
                score += 25
                reasons.append(f"tag:{tag}")

        metadata_text = " ".join(
            (
                manifest.id,
                manifest.name,
                manifest.description,
                *manifest.tags,
            )
        )
        overlap = sorted(query_terms.intersection(_terms(metadata_text.casefold())))
        if overlap:
            score += min(30, len(overlap) * 6)
            reasons.append("terms:" + ",".join(overlap[:5]))
        return score, tuple(reasons)


def _terms(text: str) -> set[str]:
    terms: set[str] = set()
    for match in _WORD_RE.finditer(text):
        token = match.group(0).casefold()
        if any("\u3400" <= char <= "\u9fff" for char in token):
            if len(token) == 1:
                terms.add(token)
            else:
                terms.update(
                    token[index : index + 2]
                    for index in range(len(token) - 1)
                )
            continue
        if len(token) >= 2 and token not in _LATIN_STOP_WORDS:
            terms.add(token)
    return terms


def _truncate(value: str, limit: int) -> tuple[str, bool]:
    if len(value) <= limit:
        return value, False
    marker = "\n[...skill truncated by host...]"
    if limit <= len(marker):
        return marker[:limit], True
    return value[: limit - len(marker)] + marker, True
