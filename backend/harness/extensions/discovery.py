"""Helpers for the extension discovery workflow.

When the user asks for a capability the agent doesn't have, the agent
should:

1. Look at the harness inventory for an existing skill / MCP that
   already does the job (no install needed).
2. If nothing fits, use ``web_search`` to find a candidate MCP server
   or skill package, prefer well-maintained repos.
3. Propose **one** candidate to the user with a one-line reason and
   wait for an explicit yes/no before installing.
4. On confirmation, route through the existing ``mcp_manage`` /
   ``skill_manage`` tools — those already handle the CONFIRM tier and
   the persistence side.

The actual discovery is driven by the LLM following the system prompt;
this module only provides helpers for unit-testable formatting and
keyword matching of the "is this even needed?" pre-check.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence


@dataclass(slots=True)
class CandidateProposal:
    """A single install candidate the agent surfaces to the user.

    The fields map directly to the ``mcp_manage`` ``add`` action so the
    proposal text and the actual install share the same shape.
    """

    kind: str             # "mcp" | "skill"
    name: str             # short identifier
    reason: str           # one-line "why this one"
    source_url: str = ""  # optional canonical link (npm / github / SKILL.md)
    transport: str = ""   # for MCP: stdio | http
    command: str = ""     # for MCP stdio: argv head
    args: list[str] = field(default_factory=list)
    description: str = ""

    def short_label(self) -> str:
        url = f" ({self.source_url})" if self.source_url else ""
        return f"{self.kind}:{self.name}{url}"


def format_install_proposal(
    user_intent: str,
    candidate: CandidateProposal,
    *,
    alternative_count: int = 0,
) -> str:
    """Render a single-candidate install proposal for the IM channel.

    Always single-candidate — the workflow forbids "pick one of three"
    prompts because users in IM rarely want to read a list. If there
    were alternatives the agent rejected, mention the count without
    listing them.
    """
    head = f"为了「{user_intent.strip()}」我想装这个："
    body = f"  • {candidate.short_label()}\n    理由: {candidate.reason}"
    if candidate.description:
        body += f"\n    简介: {candidate.description.strip()}"
    alt_line = ""
    if alternative_count > 0:
        alt_line = f"\n(另有 {alternative_count} 个候选已被排除)"
    return (
        f"{head}\n{body}{alt_line}\n\n"
        f"装吗？回复 yes / no。"
    )


def looks_like_capability_request(text: str) -> bool:
    """Heuristic: does ``text`` smell like a user asking for a new feature?

    Used by the prompt block / future routing layer to nudge the agent
    toward the discovery workflow. NOT a strict gate — false negatives
    are recovered by the agent's own judgement.
    """
    if not text:
        return False
    low = text.lower()
    triggers = (
        "你能不能", "可以做到", "可以帮我", "帮我装", "加一个功能",
        "能不能爬", "能不能下载", "支持一下", "新功能",
        "can you also", "could you add", "please install",
        "do you support", "add support for",
    )
    return any(token in low for token in triggers)


def filter_existing_inventory(
    user_intent: str,
    *,
    skill_ids: Sequence[str] = (),
    mcp_names: Sequence[str] = (),
    tool_names: Sequence[str] = (),
) -> list[str]:
    """Return inventory items whose name shares a token with ``user_intent``.

    Pure substring + token match — not a real semantic search, but good
    enough as a cheap pre-check so the agent doesn't always jump to
    web_search.
    """
    if not user_intent:
        return []
    tokens = {tok.strip().lower() for tok in user_intent.split() if len(tok) > 2}
    if not tokens:
        return []
    matches: list[str] = []
    for name in list(skill_ids) + list(mcp_names) + list(tool_names):
        low = name.lower()
        if any(tok in low for tok in tokens):
            matches.append(name)
    return matches


__all__ = [
    "CandidateProposal",
    "filter_existing_inventory",
    "format_install_proposal",
    "looks_like_capability_request",
]
