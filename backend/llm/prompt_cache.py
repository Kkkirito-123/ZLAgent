"""LLM prompt cache boundary (stable prefix vs dynamic suffix).

The boundary marker splits the assembled system prompt into:

* ``stable_prefix``  — long-lived rules/tooling descriptions safe for
  provider-side prompt caching (OpenAI ``prompt_cache_key`` / Anthropic
  ``cache_control``). Should not change between turns for the same
  session.
* ``dynamic_suffix`` — per-turn state (current time, memory snapshot,
  prefetched hints). Placed after the boundary so the provider caches
  the stable prefix once and only re-reads the dynamic suffix.

Stripping happens in the LLM client before the wire request goes out
(providers do not understand the sentinel).

Belongs to the LLM infrastructure layer: this is an LLM-provider
caching concern, not a control-layer concern. Previously lived in
``agent/system_prompt_cache.py``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

SYSTEM_PROMPT_CACHE_BOUNDARY = "\n<!-- ZLAGENT_CACHE_BOUNDARY -->\n"


@dataclass(slots=True, frozen=True)
class SystemPromptSplit:
    stable_prefix: str
    dynamic_suffix: str


def strip_system_prompt_cache_boundary(text: str) -> str:
    """Replace every boundary marker with a single newline."""
    if not text:
        return text
    return text.replace(SYSTEM_PROMPT_CACHE_BOUNDARY, "\n")


def split_system_prompt_cache_boundary(text: str) -> Optional[SystemPromptSplit]:
    """Split on the first boundary marker; return ``None`` when absent."""
    if not text:
        return None
    idx = text.find(SYSTEM_PROMPT_CACHE_BOUNDARY)
    if idx < 0:
        return None
    return SystemPromptSplit(
        stable_prefix=text[:idx].rstrip(),
        dynamic_suffix=text[idx + len(SYSTEM_PROMPT_CACHE_BOUNDARY):].lstrip(),
    )


def assemble_system_prompt(
    stable_prefix: str,
    dynamic_suffix: Optional[str] = None,
) -> str:
    """Join ``stable_prefix`` and ``dynamic_suffix`` around the boundary.

    Empty dynamic suffix returns ``stable_prefix`` verbatim (no boundary
    injected). Empty stable prefix is also supported.
    """
    stable = (stable_prefix or "").rstrip()
    dynamic = (dynamic_suffix or "").strip()
    if not dynamic:
        return stable
    if not stable:
        return dynamic
    return f"{stable}{SYSTEM_PROMPT_CACHE_BOUNDARY}{dynamic}"


def inject_after_cache_boundary(
    system_prompt: str,
    addition: Optional[str],
) -> str:
    """Prepend ``addition`` into the dynamic segment of ``system_prompt``.

    If no boundary is present, ``addition`` is prepended to the whole
    prompt so cache stability is not broken by accidentally mixing
    static and dynamic content. Empty/blank ``addition`` is returned
    unchanged.
    """
    block = (addition or "").strip()
    if not block:
        return system_prompt
    split = split_system_prompt_cache_boundary(system_prompt)
    if split is None:
        return f"{block}\n\n{system_prompt}"
    if not split.dynamic_suffix:
        return assemble_system_prompt(split.stable_prefix, block)
    return assemble_system_prompt(
        split.stable_prefix,
        f"{block}\n\n{split.dynamic_suffix}",
    )


__all__ = [
    "SYSTEM_PROMPT_CACHE_BOUNDARY",
    "SystemPromptSplit",
    "assemble_system_prompt",
    "inject_after_cache_boundary",
    "split_system_prompt_cache_boundary",
    "strip_system_prompt_cache_boundary",
]
