"""Flash-LLM intent router (v0.40.6).

A second, smaller LLM (e.g. ``deepseek-v4-flash``) reads the user
message and the catalog of available **knowledge modes** and **skills**,
then returns a small JSON object naming the best fit(s) for each. The
main :class:`~backend.agent.loop.AgentLoop` injects that decision into
the Pro model's system prompt as a hint block -- the Pro model still
owns the final choice; the router only narrows the field.

Why a second LLM and not embeddings?

* **Zero new dependencies.** We already speak the OpenAI-compatible
  protocol. Adding embeddings means pinning a model, a vector store,
  an ingestion job, plus the operational tail. A Flash LLM call costs
  the same to wire as a regular ``chat()``.
* **Reads intent, not just surface form.** The same paper request in
  Chinese and English should hit the same ai-paper mode. Small
  embedding models get this wrong far more often than DeepSeek V4
  Flash does.
* **Catalog updates instantly.** Add a new knowledge_mode -> next
  router call sees it. No re-index step.

Design constraints, in priority order:

1. **Fail-soft.** Router timeout / non-JSON / provider 5xx must return
   an empty :class:`RouterDecision`. The Pro model then runs against
   the unchanged prompt. Never block the user-visible turn on a flaky
   router.
2. **Bounded latency.** ``timeout_seconds`` (default 1.5 s) caps the
   wait. The router runs on the critical path before we even ask the
   Pro model, so a slow router directly delays TTFT.
3. **Cheap.** Prompt is short (catalog + user text), output is JSON
   <=200 tokens. Cache hits skip the network entirely.
4. **Honest.** Router prompt has explicit ``skip=true`` for empty
   intents (greetings / yes-no / unclear) so we never inject a
   misleading hint for chit-chat.

NOTE on encoding: every Chinese / non-ASCII string in this module is
written as a ``\\uXXXX`` escape sequence on purpose. The tool stack
that writes new files on Windows occasionally falls back to a
non-UTF-8 codepage and silently corrupts CJK literals -- using
escapes makes the source resilient to that bug class.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from loguru import logger

from ...llm.openai_compatible import LLMClient, LLMMessage

# -----------------------------------------------------------------------------
# Public types -- what the AgentLoop sees
# -----------------------------------------------------------------------------


@dataclass(slots=True)
class ModeChoice:
    """A single knowledge-mode pick the router believes fits the message."""

    mode_id: str
    confidence: float
    why: str = ""


@dataclass(slots=True)
class SkillChoice:
    """A single skill pick."""

    skill_id: str
    confidence: float
    why: str = ""


@dataclass(slots=True)
class RouterDecision:
    """Router output. ``skip=True`` means the router refused to recommend
    anything (typical for greetings / yes-no / unclear intent). When
    ``skip`` is True the lists are empty and the AgentLoop should leave
    the prompt untouched."""

    modes: list[ModeChoice] = field(default_factory=list)
    skills: list[SkillChoice] = field(default_factory=list)
    skip: bool = False
    skipped_reason: str = ""
    duration_ms: int = 0
    cache_hit: bool = False

    @property
    def empty(self) -> bool:
        return not self.modes and not self.skills


def empty_decision(reason: str = "disabled") -> RouterDecision:
    """Helper for the AgentLoop's fail-soft path."""

    return RouterDecision(skip=True, skipped_reason=reason)


# -----------------------------------------------------------------------------
# Catalog input shape -- the AgentLoop builds these from live registries
# -----------------------------------------------------------------------------


@dataclass(slots=True)
class ModeCatalogEntry:
    mode_id: str
    template: str
    title: str
    description: str


@dataclass(slots=True)
class SkillCatalogEntry:
    skill_id: str
    name: str
    description: str
    tags: tuple[str, ...] = ()


# -----------------------------------------------------------------------------
# Skip rules -- anything we can decide without paying for a router call
# -----------------------------------------------------------------------------

_COMMAND_RE = re.compile(r"^\s*/(reset|new|help|clear|status)\b", re.IGNORECASE)

# Unicode-escaped Chinese yes/no tokens to keep this file pure-ASCII.
# Glosses (decimal):
#   \u597d        = "good"               \u597d\u7684     = "OK / yes"
#   \u884c        = "OK / fine"          \u53ef\u4ee5     = "can / OK"
#   \u55ef        = "mm / yes"           \u55ef\u55ef     = "mm-mm / yes"
#   \u4e0d        = "no"                 \u4e0d\u8981     = "don't want"
#   \u7b97\u4e86  = "forget it"          \u53d6\u6d88     = "cancel"
#   \u7ee7\u7eed  = "continue"
_YES_NO_RE = re.compile(
    r"^\s*("
    r"yes|no|y|n|ok"
    r"|\u597d|\u597d\u7684"
    r"|\u884c|\u53ef\u4ee5"
    r"|\u55ef|\u55ef\u55ef"
    r"|\u4e0d|\u4e0d\u8981"
    r"|\u7b97\u4e86|\u53d6\u6d88|\u7ee7\u7eed"
    r")\s*[\u3002.!]?\s*$",
    re.IGNORECASE,
)
_MIN_INPUT_CHARS = 3


def _should_skip(user_text: str) -> Optional[str]:
    """Return the reason to skip, or ``None`` to actually call the router.

    Order matters: ``command`` and ``yes_no`` checks must run BEFORE
    the ``too_short`` length gate because every yes/no token in our
    dictionary is only 1-2 chars (``OK``, ``\u597d`` = "good",
    ``\u4e0d`` = "no"), and ``/reset`` is exactly 6 chars but its 1-char
    prefix ``/`` won't trip the gate either way. Reordering let the v0.40.6
    smoke pass without raising the floor; raising the floor would
    silently route real short queries like ``\u4e0a\u6d77\u5929\u6c14``
    ("Shanghai weather") through the router unnecessarily.
    """

    text = (user_text or "").strip()
    if not text:
        return "empty_input"
    if _COMMAND_RE.match(text):
        return "command"
    if _YES_NO_RE.match(text):
        return "yes_no"
    if len(text) < _MIN_INPUT_CHARS:
        return "too_short"
    return None


# -----------------------------------------------------------------------------
# Prompt template (Chinese kept as \u escapes for encoding-resilience)
# -----------------------------------------------------------------------------

# This whole prompt is written in Chinese in the on-the-wire form; we
# build it from \u escapes so the source file stays pure ASCII even if
# a future tool re-saves it under a non-UTF-8 codepage.
_ROUTER_SYSTEM_PROMPT = (
    "\u4f60\u662f ZLAgent \u7684\u610f\u56fe\u8def\u7531\u5206\u7c7b\u5668"
    "\uff0c\u53ea\u8d1f\u8d23\u6311\u51fa\u4e0e\u7528\u6237\u6d88\u606f"
    "\u6700\u5339\u914d\u7684\u77e5\u8bc6\u5e93\uff08knowledge_mode\uff09"
    "\u548c\u6280\u80fd\uff08skill\uff09\u3002\n\n"
    "\u4e25\u683c\u8981\u6c42\uff1a\n"
    "1. \u53ea\u8f93\u51fa JSON\uff0c\u65e0\u4efb\u4f55\u5176\u4ed6\u6587\u5b57"
    "\u3001Markdown\u3001\u89e3\u91ca\u3002\n"
    "2. JSON \u7ed3\u6784\u5982\u4e0b\uff1a\n"
    "{\n"
    "  \"skip\": false,\n"
    "  \"modes\":  [{\"id\": \"<mode_id>\",  \"confidence\": 0.0-1.0,"
    " \"why\": \"<10\u5b57\u5185>\"}],\n"
    "  \"skills\": [{\"id\": \"<skill_id>\", \"confidence\": 0.0-1.0,"
    " \"why\": \"<10\u5b57\u5185>\"}]\n"
    "}\n"
    "3. \u7528\u6237\u6d88\u606f\u6ca1\u6709\u660e\u786e\u9886\u57df\u610f"
    "\u56fe\u65f6\uff08\u95f2\u804a / \u6253\u62db\u547c / \u65e0\u4e3b\u9898"
    "\uff09\u2192 ``skip: true``\uff0c``modes`` \u548c ``skills`` \u90fd\u662f"
    "\u7a7a\u6570\u7ec4\u3002\n"
    "4. \u4ec5\u4ece\u4e0b\u65b9\u5019\u9009 id \u4e2d\u9009\uff1b\u4e0d\u8981"
    "\u53d1\u660e\u65b0\u7684 id\u3002\n"
    "5. ``modes`` \u6700\u591a\u8fd4\u56de 2 \u4e2a\uff0c``skills`` \u6700\u591a"
    "\u8fd4\u56de 2 \u4e2a\uff0c\u6309 confidence \u964d\u5e8f\u3002\n"
    "6. ``confidence < 0.4`` \u7684\u4e0d\u8981\u8fd4\u56de\u3002\n"
    "7. ``why`` \u5fc5\u987b\u7528\u4e2d\u6587\u7b80\u8ff0\u5339\u914d\u7406"
    "\u7531\uff0c\u4e0d\u8d85\u8fc7 10 \u4e2a\u5b57\u3002\n"
)


def _build_catalog_block(
    modes: Sequence[ModeCatalogEntry],
    skills: Sequence[SkillCatalogEntry],
) -> str:
    """Render the catalog as plain text. One line per candidate -- the
    router does NOT need the full schema.md, just the description."""

    lines: list[str] = []
    if modes:
        lines.append("## \u5019\u9009\u77e5\u8bc6\u5e93")  # Candidate KBs
        for m in modes:
            desc = (m.description or "").replace("\n", " ").strip()
            if len(desc) > 80:
                desc = desc[:80] + "\u2026"  # ellipsis
            lines.append(
                f"- {m.mode_id} [{m.template}] {m.title} -- {desc}"
            )
    if skills:
        lines.append("")
        lines.append("## \u5019\u9009 Skills")
        for s in skills:
            desc = (s.description or "").replace("\n", " ").strip()
            if len(desc) > 80:
                desc = desc[:80] + "\u2026"
            tag_str = f" ({','.join(s.tags)})" if s.tags else ""
            lines.append(f"- {s.skill_id}{tag_str} -- {desc}")
    return "\n".join(lines)


def _build_user_prompt(
    user_text: str,
    modes: Sequence[ModeCatalogEntry],
    skills: Sequence[SkillCatalogEntry],
) -> str:
    catalog = _build_catalog_block(modes, skills)
    # \u7528\u6237\u6d88\u606f = "User message"
    # \u8bf7\u6309\u8981\u6c42\u8f93\u51fa JSON\uff1a = "Please output JSON as required:"
    return (
        f"{catalog}\n\n"
        "## \u7528\u6237\u6d88\u606f\n"
        f"{user_text.strip()}\n\n"
        "\u8bf7\u6309\u8981\u6c42\u8f93\u51fa JSON\uff1a"
    )


# -----------------------------------------------------------------------------
# JSON extraction -- Flash models occasionally wrap JSON in ```json fences
# -----------------------------------------------------------------------------

_JSON_FENCE_RE = re.compile(
    r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE
)


def _extract_json_object(raw: str) -> Optional[dict[str, Any]]:
    if not raw:
        return None
    text = raw.strip()
    # First try the cheap path: bare JSON.
    if text.startswith("{") and text.endswith("}"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    # Otherwise look for a ```json fence.
    fence = _JSON_FENCE_RE.search(text)
    if fence:
        try:
            return json.loads(fence.group(1))
        except json.JSONDecodeError:
            return None
    # Last resort: scan for the first balanced { ... } block.
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _parse_decision(
    raw: str,
    *,
    mode_ids: set[str],
    skill_ids: set[str],
) -> RouterDecision:
    """Parse the router's JSON output into a :class:`RouterDecision`.

    Unknown ids are filtered out (Flash models occasionally hallucinate
    new ones despite the explicit instruction). Confidence is clamped
    to [0, 1]. Returns an empty decision on any parse error so the
    AgentLoop never crashes on a malformed router reply.
    """

    obj = _extract_json_object(raw)
    if obj is None:
        return RouterDecision(skip=True, skipped_reason="invalid_json")
    if obj.get("skip") is True:
        return RouterDecision(skip=True, skipped_reason="router_skip")

    modes: list[ModeChoice] = []
    for entry in (obj.get("modes") or [])[:2]:
        if not isinstance(entry, dict):
            continue
        mode_id = str(entry.get("id") or "").strip()
        if not mode_id or mode_id not in mode_ids:
            continue
        try:
            conf = float(entry.get("confidence") or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        conf = max(0.0, min(1.0, conf))
        if conf < 0.4:
            continue
        modes.append(
            ModeChoice(
                mode_id=mode_id,
                confidence=conf,
                why=str(entry.get("why") or ""),
            )
        )

    skills: list[SkillChoice] = []
    for entry in (obj.get("skills") or [])[:2]:
        if not isinstance(entry, dict):
            continue
        skill_id = str(entry.get("id") or "").strip()
        if not skill_id or skill_id not in skill_ids:
            continue
        try:
            conf = float(entry.get("confidence") or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        conf = max(0.0, min(1.0, conf))
        if conf < 0.4:
            continue
        skills.append(
            SkillChoice(
                skill_id=skill_id,
                confidence=conf,
                why=str(entry.get("why") or ""),
            )
        )

    if not modes and not skills:
        return RouterDecision(skip=True, skipped_reason="no_matches")
    return RouterDecision(modes=modes, skills=skills)


# -----------------------------------------------------------------------------
# LRU + TTL cache (in-memory, per-process)
# -----------------------------------------------------------------------------


class _RouterCache:
    """Tiny LRU cache. Key = sha1(user_text + catalog_signature). TTL by
    wall-clock so a stale entry doesn't survive a catalog change."""

    def __init__(self, *, capacity: int = 256, ttl_seconds: float = 60.0) -> None:
        self._capacity = capacity
        self._ttl = ttl_seconds
        self._store: OrderedDict[str, tuple[float, RouterDecision]] = OrderedDict()

    @staticmethod
    def _key(user_text: str, catalog_signature: str) -> str:
        h = hashlib.sha1()
        h.update(user_text.encode("utf-8", "replace"))
        h.update(b"\x00")
        h.update(catalog_signature.encode("utf-8", "replace"))
        return h.hexdigest()

    def get(self, user_text: str, catalog_signature: str) -> Optional[RouterDecision]:
        key = self._key(user_text, catalog_signature)
        entry = self._store.get(key)
        if entry is None:
            return None
        deadline, decision = entry
        if deadline < time.monotonic():
            self._store.pop(key, None)
            return None
        # Mark as recently used.
        self._store.move_to_end(key)
        return decision

    def set(
        self,
        user_text: str,
        catalog_signature: str,
        decision: RouterDecision,
    ) -> None:
        key = self._key(user_text, catalog_signature)
        deadline = time.monotonic() + self._ttl
        self._store[key] = (deadline, decision)
        self._store.move_to_end(key)
        while len(self._store) > self._capacity:
            self._store.popitem(last=False)

    def clear(self) -> None:
        self._store.clear()


# -----------------------------------------------------------------------------
# Public class
# -----------------------------------------------------------------------------


class RouterLLM:
    """Flash-LLM router. Sole entry point is :meth:`route`.

    Construct with a *separate* :class:`LLMClient` instance configured
    for the Flash model -- never share the main Pro client because we
    want different temperature / max_tokens / streaming settings. Use
    :func:`build_router_settings` (in ``app.py``) to derive the Flash
    settings from the main settings.
    """

    def __init__(
        self,
        client: LLMClient,
        *,
        timeout_seconds: float = 1.5,
        cache_ttl_seconds: float = 60.0,
    ) -> None:
        self._client = client
        self._timeout = max(0.1, float(timeout_seconds))
        self._cache = _RouterCache(ttl_seconds=cache_ttl_seconds)

    @property
    def model_name(self) -> str:
        return self._client.model

    @staticmethod
    def _catalog_signature(
        modes: Sequence[ModeCatalogEntry],
        skills: Sequence[SkillCatalogEntry],
    ) -> str:
        """Cheap deterministic fingerprint so the cache invalidates when
        the operator adds / removes a knowledge_mode or a skill."""

        parts: list[str] = []
        for m in modes:
            parts.append(f"M:{m.mode_id}:{m.template}")
        for s in skills:
            parts.append(f"S:{s.skill_id}")
        return "|".join(sorted(parts))

    async def route(
        self,
        user_text: str,
        *,
        modes: Sequence[ModeCatalogEntry] = (),
        skills: Sequence[SkillCatalogEntry] = (),
    ) -> RouterDecision:
        """Return the router's best guess for the user message.

        Always returns a :class:`RouterDecision`; callers do not need
        to handle exceptions. On any failure path (timeout / non-JSON /
        client not configured) the returned decision has ``skip=True``.
        """

        skip_reason = _should_skip(user_text)
        if skip_reason is not None:
            return RouterDecision(skip=True, skipped_reason=skip_reason)
        if not modes and not skills:
            return RouterDecision(skip=True, skipped_reason="empty_catalog")
        if not self._client.configured:
            return RouterDecision(skip=True, skipped_reason="router_not_configured")

        catalog_signature = self._catalog_signature(modes, skills)
        cached = self._cache.get(user_text, catalog_signature)
        if cached is not None:
            return RouterDecision(
                modes=list(cached.modes),
                skills=list(cached.skills),
                skip=cached.skip,
                skipped_reason=cached.skipped_reason,
                duration_ms=cached.duration_ms,
                cache_hit=True,
            )

        messages = [
            LLMMessage(role="system", content=_ROUTER_SYSTEM_PROMPT),
            LLMMessage(
                role="user",
                content=_build_user_prompt(user_text, modes, skills),
            ),
        ]
        mode_ids = {m.mode_id for m in modes}
        skill_ids = {s.skill_id for s in skills}

        started = time.monotonic()
        try:
            response = await asyncio.wait_for(
                self._client.chat(
                    messages,
                    temperature=0.0,  # deterministic routing
                    stream=False,
                ),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError:
            elapsed = int((time.monotonic() - started) * 1000)
            logger.warning(
                "[router] timeout after {}ms (model={}, budget={}s)",
                elapsed,
                self._client.model,
                self._timeout,
            )
            return RouterDecision(
                skip=True, skipped_reason="timeout", duration_ms=elapsed
            )
        except Exception as exc:  # noqa: BLE001 - fail-soft on any provider error
            elapsed = int((time.monotonic() - started) * 1000)
            logger.warning(
                "[router] chat failed in {}ms: {}: {}",
                elapsed,
                type(exc).__name__,
                exc,
            )
            return RouterDecision(
                skip=True,
                skipped_reason=f"error:{type(exc).__name__}",
                duration_ms=elapsed,
            )

        elapsed = int((time.monotonic() - started) * 1000)
        decision = _parse_decision(
            response.content or "",
            mode_ids=mode_ids,
            skill_ids=skill_ids,
        )
        decision.duration_ms = elapsed

        # Cache both positive and "no_matches" outcomes -- the catalog
        # signature is part of the key so adding a new mode invalidates.
        self._cache.set(user_text, catalog_signature, decision)

        if decision.skip:
            logger.debug(
                "[router] skip={} (reason={}) in {}ms",
                decision.skip,
                decision.skipped_reason,
                elapsed,
            )
        else:
            logger.info(
                "[router] picked modes={} skills={} in {}ms",
                [m.mode_id for m in decision.modes],
                [s.skill_id for s in decision.skills],
                elapsed,
            )
        return decision

    def clear_cache(self) -> None:
        """Drop every cached decision. Useful for smoke tests."""

        self._cache.clear()


# -----------------------------------------------------------------------------
# Settings factory -- keep the second-LLMClient build out of app.py
# -----------------------------------------------------------------------------


def build_router_settings(main_settings: Any) -> Any:
    """Derive a :class:`backend.core.config.Settings` instance suitable
    for the Flash router from the main settings.

    The router needs:
      * different ``openai_model`` (the Flash one)
      * usually the same ``openai_base_url`` and ``openai_api_key``
        (most setups use the same provider for both Pro and Flash)
      * ``llm_temperature = 0`` and a small ``llm_max_tokens`` (the
        router only emits ~150 tokens of JSON; keeping the cap small
        truncates run-away outputs early)
      * ``llm_stream_enabled = False`` -- we want the full JSON in one
        shot to parse it
      * ``prompt_cache_enabled = False`` -- every user message is
        different; provider-side cache would not help here

    Returns the *same* class as ``main_settings`` (pydantic model_copy),
    so the resulting object can be fed straight into ``LLMClient``.
    """

    overrides: dict[str, Any] = {
        "openai_model": main_settings.router_llm_model.strip(),
        "llm_temperature": 0.0,
        "llm_max_tokens": 512,
        "llm_stream_enabled": False,
        "prompt_cache_enabled": False,
    }
    base_url = (main_settings.router_llm_base_url or "").strip()
    if base_url:
        overrides["openai_base_url"] = base_url
    api_key = (main_settings.router_llm_api_key or "").strip()
    if api_key:
        overrides["openai_api_key"] = api_key
    return main_settings.model_copy(update=overrides)

