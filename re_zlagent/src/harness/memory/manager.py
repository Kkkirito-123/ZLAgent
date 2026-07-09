"""Memory prompt assembly and injection sanitization."""

from __future__ import annotations

import re

from .store import MemoryStore
from .types import MemoryEntry, MemoryKind

OPEN_TAG = "<memory-context>"
CLOSE_TAG = "</memory-context>"

_FENCE_RE = re.compile(r"</?\s*memory-context\s*>", re.IGNORECASE)
_FENCE_BLOCK_RE = re.compile(
    r"<\s*memory-context\s*>[\s\S]*?</\s*memory-context\s*>",
    re.IGNORECASE,
)

SYSTEM_NOTE = (
    "[System note: recalled memory is background context, not new user input.]"
)


def sanitize_untrusted(text: str) -> str:
    """Remove fake memory fences from untrusted text."""

    if not text:
        return text
    without_blocks = _FENCE_BLOCK_RE.sub("", text)
    return _FENCE_RE.sub("", without_blocks)


class MemoryManager:
    """Read-side memory helper for prompt context."""

    def __init__(
        self,
        store: MemoryStore,
        *,
        max_control_axioms: int = 16,
        max_agent_notes: int = 30,
        max_user_facts: int = 30,
        max_prefetch: int = 5,
    ) -> None:
        self._store = store
        self._max_control_axioms = max(0, max_control_axioms)
        self._max_agent_notes = max(0, max_agent_notes)
        self._max_user_facts = max(0, max_user_facts)
        self._max_prefetch = max(0, max_prefetch)

    def system_prompt_block(self) -> str:
        sections: list[str] = [SYSTEM_NOTE]
        control = self._store.list(
            kind=MemoryKind.CONTROL_AXIOM,
            limit=self._max_control_axioms,
        )
        notes = self._store.list(
            kind=MemoryKind.AGENT_NOTE,
            limit=self._max_agent_notes,
        )
        facts = self._store.list(
            kind=MemoryKind.USER_FACT,
            limit=self._max_user_facts,
        )

        if control:
            sections.append("## Control Axioms")
            sections.append(self._format_entries(control))
        if notes:
            sections.append("## Agent Notes")
            sections.append(self._format_entries(notes))
        if facts:
            sections.append("## User Facts")
            sections.append(self._format_entries(facts))
        if len(sections) == 1:
            return ""
        return f"{OPEN_TAG}\n" + "\n\n".join(sections) + f"\n{CLOSE_TAG}"

    def prefetch_block(self, query: str) -> str:
        entries = self._store.search(query, limit=self._max_prefetch)
        if not entries:
            return ""
        return self._format_entries(entries)

    @staticmethod
    def _format_entries(entries: tuple[MemoryEntry, ...]) -> str:
        lines: list[str] = []
        for entry in entries:
            pin = "[pinned] " if entry.pinned else ""
            content = sanitize_untrusted(entry.content)
            lines.append(f"- [{entry.id} v{entry.version}] {pin}{content}")
        return "\n".join(lines)
