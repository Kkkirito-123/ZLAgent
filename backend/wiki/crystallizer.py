"""Atomic-fact crystallization for the wiki cache.

Borrowed from the LLM Wiki v2 gist's "compounding from exploration"
pattern: after the main agent answers a question, a small follow-up
LLM call distils 3-5 *atomic facts* out of that answer and stores
each one as its own wiki row with ``crystal_kind="atomic_fact"``.

Why bother:

* The legacy v0.37 wiki cache only matches on the user's *exact*
  question. "京沪高铁多久" hits the cache; "北京到上海高铁需要几个
  小时" does not, even though the cached answer contains the fact.
* Atomic-fact rows store one claim each (``answer="约 4.5 小时"``)
  alongside 2-4 paraphrase ``aliases`` ("京沪高铁多久" /
  "北京到上海高铁时长" / "京沪高铁运行时间"). Future short factual
  queries hit the atomic row directly without a full LLM
  round-trip — Wiki v2's "recall, don't regenerate".
* Each atomic row carries ``sources=["wiki_entry:<answer_id>"]``
  so an operator can trace where a fact came from. When an answer
  is retracted, all its atomic facts can be retracted in one
  cascading sweep (future feature; v0.39 only writes the
  provenance, doesn't yet act on it).

The pipeline is **opt-in per skill** via
``SkillManifest.crystallize=True`` and **fire-and-forget** in the
agent loop — the user already received their answer; failure here
costs at most a missed cache opportunity.

Cost: ~700 tokens per crystallization call on DeepSeek (300 input
prompt + 400 output JSON), so leave it off for chatty skills and on
for query-type skills (travel-guide, weather-now, news-digest).
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional, Sequence

from loguru import logger

if TYPE_CHECKING:
    from ..llm.openai_compatible import LLMClient
    from .store import WikiStore
    # GeoStore is the rule-based fallback when the LLM
    # forgets to fill in ``geo_path``. Annotation-only import.
    from .geo_store import GeoStore


# The fact-extraction prompt. Kept short to bound token cost; the
# strict JSON schema lets us parse without a forgiving NLP pass.
# IMPORTANT: this prompt is only ever shown to the *crystallization*
# LLM, never to the user. Don't share style with user-facing
# system prompts (those try to be conversational; this one is
# purely structural).
_CRYSTALLIZE_PROMPT = (
    "你是知识沉淀助手。给你一段助手回答，请抽取其中**稳定、可独立验证**的"
    "原子事实。每条事实满足：\n"
    "1. 自包含（脱离原回答仍然完整）；\n"
    "2. 时效稳定（不要抽实时车次/股价/新闻头条）；\n"
    "3. 不要抽营销话术、主观评价、冗长描述。\n"
    "\n"
    "输出严格的 JSON：\n"
    "```json\n"
    "{\n"
    "  \"facts\": [\n"
    "    {\n"
    "      \"claim\": \"<事实陈述，10-40 字>\",\n"
    "      \"queries\": [\"<最常见的查询写法>\", \"<同义改写1>\", \"<同义改写2>\"],\n"
    "      \"confidence\": <0.4-0.8 之间的浮点>,\n"
    "      \"cities\": [\"<该事实涉及的市级地名>\", ...]\n"
    "    },\n"
    "    ...\n"
    "  ]\n"
    "}\n"
    "```\n"
    "\n"
    "硬性规则：\n"
    "* facts 数量 3-5 条。如果回答里没有可抽的稳定事实，输出 ``{\"facts\": []}``。\n"
    "* queries 至少 2 条；第一条是规范化查询（短、关键词为主），\n"
    "  其余是同义改写（不同表达同一个意思）。\n"
    "* confidence 给 0.4-0.8。事实越确切（数字、规范、名称）越靠近 0.8；\n"
    "  推断/估算靠近 0.4。绝不输出 > 0.8 — 这是模型自评，留给后续 hit 强化。\n"
    "* cities 是该条事实明确涉及的**市级**中文地名数组，如 [\"上海\"], \n"
    "  [\"北京\", \"上海\"]。不肯定或与地点无关输出 []。使用市级名称，\n"
    "  不要“上海外滩”这种增加区/景点后缀。宁可不加也不要乱加。\n"
    "* 整段必须只有一段 ```json ... ``` 代码块，外面不要再写解释。\n"
)


_FENCE_RE = re.compile(
    r"```(?:json)?\s*\n(?P<body>\{.*?\})\s*\n```",
    re.DOTALL,
)


@dataclass(slots=True)
class AtomicFact:
    """One extracted fact, ready to write to the wiki."""

    claim: str
    queries: tuple[str, ...]  # primary first, paraphrases follow
    confidence: float
    # raw city names from the LLM. Empty when the LLM
    # didn't return a ``cities`` field or when none of the names
    # resolve. The Crystallizer maps these to ``geo_path`` tokens
    # via :class:`backend.wiki.geo_store.GeoStore.find_by_name`
    # before writing to the wiki, so a non-empty ``cities`` here
    # does NOT guarantee a non-empty written ``geo_path`` (the LLM
    # might hallucinate "火星市").
    cities: tuple[str, ...] = ()


def _parse_facts(raw: str) -> list[AtomicFact]:
    """Pull the ``{"facts": [...]}`` JSON out of an LLM response.

    Returns an empty list on any parse failure — crystallization is
    advisory, never a hard error.
    """
    if not raw:
        return []
    body: Optional[str] = None
    match = _FENCE_RE.search(raw)
    if match:
        body = match.group("body")
    else:
        # Some providers omit the ``json`` info-string or even the
        # whole fence. Try to grab the first balanced ``{...}`` blob.
        first_brace = raw.find("{")
        last_brace = raw.rfind("}")
        if 0 <= first_brace < last_brace:
            body = raw[first_brace : last_brace + 1]
    if not body:
        return []
    try:
        data = json.loads(body)
    except (ValueError, TypeError) as exc:
        logger.debug("[crystallize] JSON parse failed: {}", exc)
        return []
    if not isinstance(data, dict):
        return []
    facts = data.get("facts")
    if not isinstance(facts, list):
        return []
    out: list[AtomicFact] = []
    for raw_fact in facts:
        if not isinstance(raw_fact, dict):
            continue
        claim = str(raw_fact.get("claim") or "").strip()
        queries_raw = raw_fact.get("queries") or []
        if not isinstance(queries_raw, list):
            continue
        queries = tuple(
            str(q).strip() for q in queries_raw if str(q).strip()
        )
        if not claim or len(queries) < 1:
            continue
        try:
            confidence = float(raw_fact.get("confidence", 0.5))
        except (ValueError, TypeError):
            confidence = 0.5
        # Defensive clamp; the LLM occasionally over-confidences.
        confidence = max(0.05, min(0.8, confidence))
        # ``cities`` is optional. Missing or non-list
        # values just yield an empty tuple; the Crystallizer will
        # fall back to GeoStore.detect_in_text on the claim.
        cities_raw = raw_fact.get("cities") or []
        if not isinstance(cities_raw, list):
            cities_raw = []
        cities = tuple(
            str(c).strip() for c in cities_raw if str(c).strip()
        )
        out.append(AtomicFact(
            claim=claim,
            queries=queries,
            confidence=confidence,
            cities=cities,
        ))
    return out


class Crystallizer:
    """Extract atomic facts from an LLM answer and write them to wiki.

    Stateless — safe to share one instance across the whole agent.
    The actual LLM call goes through whatever ``LLMClient`` is wired
    up, so smoke tests can swap in a fake client to exercise the
    parsing + write paths without burning real tokens.
    """

    def __init__(
        self,
        *,
        llm: "LLMClient",
        wiki_store: "WikiStore",
        max_facts_per_call: int = 5,
        per_call_token_budget: int = 600,
        # GeoStore is optional. When provided, the
        # crystallizer can (a) translate LLM-returned city names
        # like ``["上海", "北京"]`` into canonical
        # ``geo_path`` tokens (``"city:上海;city:北京"``), and
        # (b) fall back to :meth:`GeoStore.detect_in_text` on the
        # ``claim`` when the LLM forgot to populate ``cities``.
        # When ``None``, atomic facts are written WITHOUT geo_path
        # — i.e. byte-equivalent to the v0.39 behaviour.
        geo_store: Optional["GeoStore"] = None,
    ) -> None:
        self._llm = llm
        self._wiki = wiki_store
        self._geo = geo_store
        self._max_facts_per_call = max(1, int(max_facts_per_call))
        # Hint to the LLM client; some providers honour
        # ``max_tokens`` on output and we want to bound the cost.
        # The crystallizer is meant to be cheap.
        self._token_budget = max(128, int(per_call_token_budget))

    @property
    def configured(self) -> bool:
        """Mirror :attr:`LLMClient.configured` so callers can gate."""
        return bool(self._llm and getattr(self._llm, "configured", False))

    def _resolve_geo_path(self, fact: AtomicFact) -> str:
        """produce a wiki ``geo_path`` for one atomic fact.

        The pipeline is **two-stage with a hard cap**:

        1. **LLM-supplied ``cities``**: each name is resolved through
           :meth:`GeoStore.find_by_name` with ``type_="city"``. Hits
           become canonical tokens via :meth:`GeoStore.geo_path_for`.
           Hallucinated names (no GeoStore hit) are silently dropped
           — we never write a ``geo_path`` token for an entity we
           don't recognise.
        2. **Greedy text scan fallback**: when stage 1 produced no
           tokens (LLM omitted the field, or all names hallucinated),
           we run :meth:`GeoStore.detect_in_text` on ``fact.claim``
           and use whatever it finds. This catches cases where the
           LLM forgot the structured field but the claim itself
           clearly mentions a city.

        Returns the ``";"``-joined token string, or ``""`` when no
        GeoStore is wired or no token resolves. Capped at 4 tokens
        per fact (a single atomic fact almost never legitimately
        spans more than two cities; 4 is a generous safety margin).
        """
        if self._geo is None:
            return ""
        max_tokens = 4
        nodes: list = []
        seen_ids: set[int] = set()

        # Stage 1 — trust the LLM first. Resolve each city name and
        # only keep those that are actually known cities; this is
        # the "fast path" that avoids re-scanning the claim text.
        for raw_name in fact.cities:
            if len(nodes) >= max_tokens:
                break
            hits = self._geo.find_by_name(raw_name, type_="city")
            for n in hits:
                if n.id in seen_ids:
                    continue
                seen_ids.add(n.id)
                nodes.append(n)
                if len(nodes) >= max_tokens:
                    break

        # Stage 2 — text scan fallback. Only fires when stage 1
        # came up empty so we don't waste cycles on a clean LLM
        # response. ``detect_in_text`` already does longest-match
        # disambiguation and dedup.
        if not nodes:
            text_hits = self._geo.detect_in_text(
                fact.claim, max_matches=max_tokens,
            )
            for n in text_hits:
                if n.type != "city":
                    continue
                if n.id in seen_ids:
                    continue
                seen_ids.add(n.id)
                nodes.append(n)
                if len(nodes) >= max_tokens:
                    break

        if not nodes:
            return ""
        return self._geo.encode_geo_path(nodes)

    async def crystallize(
        self,
        *,
        skill_id: str,
        answer: str,
        source_entry_id: Optional[int] = None,
        ttl_seconds: Optional[int] = None,
    ) -> list[int]:
        """Extract atomic facts from ``answer`` and write them.

        Returns the list of new wiki row ids written. An empty list
        indicates either the LLM produced no extractable facts, the
        client wasn't configured, or every parsed fact failed
        validation (e.g. empty primary query).

        ``source_entry_id`` is the row id of the *answer* entry that
        produced this crystallization. It's recorded on each atomic
        row's ``sources`` array as ``"wiki_entry:<id>"`` so a
        future retract sweep can cascade.

        ``ttl_seconds`` defaults to the answer's TTL (passed by the
        caller). Atomic facts inherit TTL semantics so a skill with
        ``wiki_cache_ttl_seconds=2592000`` writes 30-day atomic
        rows alongside its 30-day answer rows.
        """
        if not self.configured:
            logger.debug(
                "[crystallize] LLM not configured; skipping skill={}",
                skill_id,
            )
            return []
        if not answer or not answer.strip():
            return []

        from ..llm.openai_compatible import LLMMessage  # local import

        messages = [
            LLMMessage(role="system", content=_CRYSTALLIZE_PROMPT),
            LLMMessage(role="user", content=answer.strip()[:4000]),
        ]
        try:
            # No tools, no streaming — keep it as a flat round-trip.
            response = await self._llm.chat(messages)
        except Exception as exc:  # noqa: BLE001 — advisory pipeline
            logger.warning(
                "[crystallize] LLM call failed skill={} err={}",
                skill_id, exc,
            )
            return []

        raw = response.content or ""
        facts = _parse_facts(raw)
        if not facts:
            logger.debug(
                "[crystallize] no facts extracted skill={} answer_len={}",
                skill_id, len(answer),
            )
            return []
        # Cap upper bound — a misbehaving LLM might return 30 trivial
        # facts; we don't want to flood the wiki.
        if len(facts) > self._max_facts_per_call:
            logger.debug(
                "[crystallize] truncating {}->{} facts skill={}",
                len(facts), self._max_facts_per_call, skill_id,
            )
            facts = facts[: self._max_facts_per_call]

        # Build a sources list for provenance. Empty if the caller
        # didn't tell us where the answer came from.
        provenance: list[str] = []
        if source_entry_id is not None:
            provenance.append(f"wiki_entry:{int(source_entry_id)}")

        written: list[int] = []
        for fact in facts:
            primary_query = fact.queries[0]
            paraphrases = list(fact.queries[1:])
            # resolve geo_path. Layer 1: trust the LLM's
            # ``cities`` if present and the names resolve. Layer 2:
            # fall back to GeoStore's text-scan on the claim.
            # Layer 3: if no GeoStore is wired or nothing resolves,
            # leave geo_path empty (matches v0.39 byte-for-byte).
            geo_path = self._resolve_geo_path(fact)
            try:
                row_id = self._wiki.add(
                    skill_id=skill_id,
                    raw_query=primary_query,
                    answer=fact.claim,
                    ttl_seconds=ttl_seconds,
                    confidence=fact.confidence,
                    sources=provenance,
                    aliases=paraphrases,
                    crystal_kind="atomic_fact",
                    geo_path=geo_path,
                    metadata={
                        "crystallized_from_answer_id": source_entry_id,
                        "extracted_by": getattr(self._llm, "model", "unknown"),
                    },
                )
            except ValueError as exc:
                # Empty / unnormalizable query string. The LLM
                # occasionally returns whitespace; skip.
                logger.debug(
                    "[crystallize] skipping malformed fact skill={} err={}",
                    skill_id, exc,
                )
                continue
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[crystallize] wiki write failed skill={} err={}",
                    skill_id, exc,
                )
                continue
            written.append(row_id)

        if written:
            logger.info(
                "[crystallize] skill={} extracted {}/{} facts source={} ids={}",
                skill_id, len(written), len(facts),
                source_entry_id, written,
            )
        return written
