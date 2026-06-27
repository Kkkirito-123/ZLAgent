"""LLM-driven knowledge-graph extraction with per-record caching.

Two schemas are supported (selected by ``record.source_type``):

* ``paper`` — wiki/output pages.  Produces typed ``Paper`` / ``Keyword`` /
  ``Author`` / ``Venue`` nodes and ``has_keyword`` / ``authored_by`` /
  ``published_at`` edges.  This is what the user's screenshot needs:
  recognisable paper titles, real keywords, and source links.
* ``memory`` — UserMemory rows.  Produces ``Topic`` / ``Preference`` /
  ``Entity`` nodes and ``prefers`` / ``relates_to`` / ``contradicts``
  edges.

The extractor is **never** invoked on the synchronous request path —
``KnowledgeGraphBuilder`` only consumes already-cached results.  Cache
warm-up happens in two ways:

1. ``KnowledgeIngestTool`` schedules a background ``extract_and_cache``
   right after a successful ingest (no impact on user-visible latency).
2. ``POST /api/knowledge-bases/{id}/rebuild-graph`` walks every record
   and re-runs extraction synchronously with a 60s wall-clock cap.

Failures are fail-soft: timeouts, non-JSON output and provider errors
return ``None`` and leave the cache untouched, so the heuristic builder
still produces *something* on the next page load.
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Sequence

from loguru import logger

from ..llm.openai_compatible import LLMClient, LLMMessage
from .cache import CachedExtraction, LLMGraphCache, record_cache_key
from .source import GraphSourceRecord


# Test seam: the smoke harness can swap in a stub instead of LLMClient.
ChatFn = Callable[[Sequence[LLMMessage]], Awaitable[str]]


_PAPER_PROMPT = """你是一个学术论文知识图谱抽取助手。读取下面的论文条目，输出严格 JSON。

要求：
1. 必须只输出一个 JSON 对象，不要 Markdown、不要解释、不要 ```json fence。
2. 顶层字段：nodes, edges。
3. nodes 数组里每个元素是 {id, kind, label, attrs}。kind 必须是以下之一：
   - "Paper"   主论文节点（每条记录恰好 1 个），attrs 必须含 {title, source_url, summary, year}
   - "Keyword" 关键词（3–8 个），attrs.kind="topic"
   - "Author"  作者姓名（可 0 个），attrs.kind="person"
   - "Venue"   会议/期刊（可 0 个），attrs.kind="venue"
4. edges 数组里每个元素是 {source, target, kind}，kind 必须是以下之一：
   - "has_keyword"   Paper -> Keyword
   - "authored_by"   Paper -> Author
   - "published_at"  Paper -> Venue
5. id 用短横线小写英文/数字（slug），同名节点 id 必须一致；Paper 的 id 固定为 "paper"。
6. 不要编造作者/会议；信息缺失时直接省略对应节点和边。
7. label 用原文（中英皆可）；keywords 优先英文术语。

示例输出：
{"nodes":[{"id":"paper","kind":"Paper","label":"Attention Is All You Need","attrs":{"title":"Attention Is All You Need","source_url":"https://arxiv.org/abs/1706.03762","summary":"Transformer architecture.","year":"2017"}},{"id":"transformer","kind":"Keyword","label":"Transformer","attrs":{"kind":"topic"}}],"edges":[{"source":"paper","target":"transformer","kind":"has_keyword"}]}
"""


_MEMORY_PROMPT = """你是一个个人记忆知识图谱抽取助手。读取下面这条用户记忆，输出严格 JSON。

要求：
1. 必须只输出一个 JSON 对象，不要 Markdown、不要解释、不要 ```json fence。
2. 顶层字段：nodes, edges。
3. nodes 每个元素是 {id, kind, label, attrs}。kind 必须是以下之一：
   - "Memory"     主记忆节点（每条记录恰好 1 个），attrs 必须含 {summary, kind: user_fact|agent_note}
   - "Topic"      话题/领域（0–3 个）
   - "Preference" 用户偏好（0–3 个），label 用一句简短陈述（如"喜欢简短回复"）
   - "Entity"     人/物/地点等具名实体（0–4 个），attrs.entity_type=person|place|product|other
4. edges 每个元素是 {source, target, kind}，kind 必须是以下之一：
   - "about"        Memory -> Topic
   - "prefers"      Memory -> Preference
   - "mentions"     Memory -> Entity
   - "relates_to"   任意 -> 任意（弱关联，节制使用）
5. id 用短横线小写 slug；Memory 的 id 固定为 "memory"。
6. 不要编造内容；信息不足时省略节点/边即可，输出空 nodes/edges 也合法。

示例输出：
{"nodes":[{"id":"memory","kind":"Memory","label":"用户偏好简短回复","attrs":{"summary":"用户喜欢简短回复，不超过50字","kind":"user_fact"}},{"id":"reply-style","kind":"Preference","label":"简短回复 ≤50 字","attrs":{}}],"edges":[{"source":"memory","target":"reply-style","kind":"prefers"}]}
"""


_VALID_PAPER_NODE_KINDS = {"Paper", "Keyword", "Author", "Venue"}
_VALID_PAPER_EDGE_KINDS = {"has_keyword", "authored_by", "published_at"}
_VALID_MEMORY_NODE_KINDS = {"Memory", "Topic", "Preference", "Entity"}
_VALID_MEMORY_EDGE_KINDS = {"about", "prefers", "mentions", "relates_to"}


@dataclass(slots=True)
class LLMGraphNode:
    id: str
    kind: str
    label: str
    attrs: dict[str, Any]


@dataclass(slots=True)
class LLMGraphEdge:
    source: str
    target: str
    kind: str
    attrs: dict[str, Any]


@dataclass(slots=True)
class LLMGraphResult:
    schema: str  # "paper" | "memory"
    nodes: list[LLMGraphNode]
    edges: list[LLMGraphEdge]


def _schema_for_record(record: GraphSourceRecord) -> str:
    if record.source_type == "paper":
        return "paper"
    return "memory"


def _extract_json_object(raw: str) -> Optional[dict[str, Any]]:
    """Tolerant JSON extraction.

    Supports plain JSON, ```json fenced blocks, and prose-prefixed
    output by scanning for the first balanced ``{...}`` block.
    """
    if not raw:
        return None
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        candidate = fence.group(1)
    else:
        start = text.find("{")
        if start < 0:
            return None
        depth = 0
        end = -1
        for i in range(start, len(text)):
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end < 0:
            return None
        candidate = text[start:end]
    try:
        obj = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _slug_id(value: str, fallback: str) -> str:
    text = (value or "").strip().lower()
    text = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", text)
    text = text.strip("-")[:60]
    return text or fallback


def _coerce_result(
    payload: dict[str, Any],
    *,
    schema: str,
) -> LLMGraphResult:
    valid_node_kinds = _VALID_PAPER_NODE_KINDS if schema == "paper" else _VALID_MEMORY_NODE_KINDS
    valid_edge_kinds = _VALID_PAPER_EDGE_KINDS if schema == "paper" else _VALID_MEMORY_EDGE_KINDS

    raw_nodes = payload.get("nodes") if isinstance(payload, dict) else None
    raw_edges = payload.get("edges") if isinstance(payload, dict) else None

    nodes: list[LLMGraphNode] = []
    seen_ids: set[str] = set()
    if isinstance(raw_nodes, list):
        for idx, entry in enumerate(raw_nodes):
            if not isinstance(entry, dict):
                continue
            kind = str(entry.get("kind") or "").strip()
            if kind not in valid_node_kinds:
                continue
            label = str(entry.get("label") or entry.get("id") or "").strip()
            if not label:
                continue
            node_id = str(entry.get("id") or "").strip()
            if not node_id:
                node_id = _slug_id(label, fallback=f"{kind.lower()}-{idx}")
            if node_id in seen_ids:
                continue
            seen_ids.add(node_id)
            attrs = entry.get("attrs")
            attrs_dict = attrs if isinstance(attrs, dict) else {}
            nodes.append(LLMGraphNode(id=node_id, kind=kind, label=label[:120], attrs=dict(attrs_dict)))

    edges: list[LLMGraphEdge] = []
    edge_keys: set[tuple[str, str, str]] = set()
    if isinstance(raw_edges, list):
        for entry in raw_edges:
            if not isinstance(entry, dict):
                continue
            source = str(entry.get("source") or "").strip()
            target = str(entry.get("target") or "").strip()
            kind = str(entry.get("kind") or "").strip()
            if not source or not target or source == target:
                continue
            if kind not in valid_edge_kinds:
                continue
            if source not in seen_ids or target not in seen_ids:
                continue
            key = (source, target, kind)
            if key in edge_keys:
                continue
            edge_keys.add(key)
            attrs = entry.get("attrs")
            attrs_dict = attrs if isinstance(attrs, dict) else {}
            edges.append(LLMGraphEdge(source=source, target=target, kind=kind, attrs=dict(attrs_dict)))

    return LLMGraphResult(schema=schema, nodes=nodes, edges=edges)


def _build_user_payload(record: GraphSourceRecord, *, max_chars: int) -> str:
    body = (record.content or record.summary or "").strip()
    if len(body) > max_chars:
        body = body[:max_chars] + "…"
    fields = [
        f"id: {record.id}",
        f"title: {record.title}",
        f"source_type: {record.source_type}",
    ]
    url = str(record.attrs.get("source_url") or "").strip()
    if url:
        fields.append(f"source_url: {url}")
    if record.tags:
        tags = ", ".join(t for t in record.tags if t)
        if tags:
            fields.append(f"tags: {tags}")
    if record.summary and record.summary != body:
        fields.append(f"summary: {record.summary}")
    fields.append("body:\n" + body)
    return "\n".join(fields)


class LLMGraphExtractor:
    """LLM-backed graph extractor with caching.  All async."""

    def __init__(
        self,
        *,
        llm: Optional[LLMClient],
        cache: LLMGraphCache,
        timeout_seconds: float = 8.0,
        max_content_chars: int = 4000,
        chat_fn: Optional[ChatFn] = None,
    ) -> None:
        self._llm = llm
        self._cache = cache
        self._timeout = max(1.0, float(timeout_seconds))
        self._max_chars = max(500, int(max_content_chars))
        self._chat_fn = chat_fn

    @property
    def cache(self) -> LLMGraphCache:
        return self._cache

    @property
    def configured(self) -> bool:
        return bool(self._llm) or self._chat_fn is not None

    async def _chat(self, messages: list[LLMMessage]) -> str:
        if self._chat_fn is not None:
            return await self._chat_fn(messages)
        if self._llm is None:
            raise RuntimeError("LLMGraphExtractor: no LLM configured")
        response = await self._llm.chat(messages, temperature=0.0, stream=False)
        return response.content or ""

    async def extract(self, record: GraphSourceRecord) -> Optional[LLMGraphResult]:
        if not self.configured:
            return None
        schema = _schema_for_record(record)
        prompt = _PAPER_PROMPT if schema == "paper" else _MEMORY_PROMPT
        user_payload = _build_user_payload(record, max_chars=self._max_chars)
        messages = [
            LLMMessage(role="system", content=prompt),
            LLMMessage(role="user", content=user_payload),
        ]
        try:
            raw = await asyncio.wait_for(self._chat(messages), timeout=self._timeout)
        except asyncio.TimeoutError:
            logger.warning("[graph_llm] timeout for record={}", record.id)
            return None
        except Exception as exc:  # noqa: BLE001 — fail-soft per design
            logger.warning("[graph_llm] chat failed for record={}: {}", record.id, exc)
            return None
        payload = _extract_json_object(raw)
        if payload is None:
            logger.warning("[graph_llm] non-JSON output for record={}", record.id)
            return None
        try:
            return _coerce_result(payload, schema=schema)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[graph_llm] coerce failed for record={}: {}", record.id, exc)
            return None

    async def extract_and_cache(self, record: GraphSourceRecord) -> Optional[LLMGraphResult]:
        result = await self.extract(record)
        if result is None:
            return None
        if not result.nodes:
            return result
        key = record_cache_key(record.id, record.content or record.summary, max_chars=self._max_chars)
        cached = CachedExtraction(
            schema=result.schema,
            nodes=[{"id": n.id, "kind": n.kind, "label": n.label, "attrs": n.attrs} for n in result.nodes],
            edges=[{"source": e.source, "target": e.target, "kind": e.kind, "attrs": e.attrs} for e in result.edges],
            generated_at=LLMGraphCache.now_iso(),
            model=getattr(self._llm, "model", "") if self._llm else "stub",
        )
        try:
            self._cache.put(record.knowledge_base_id or "default", key, cached)
        except OSError as exc:
            logger.warning("[graph_llm] cache write failed for {}: {}", record.id, exc)
        return result

    def cached_for(self, record: GraphSourceRecord) -> Optional[CachedExtraction]:
        key = record_cache_key(record.id, record.content or record.summary, max_chars=self._max_chars)
        return self._cache.get(record.knowledge_base_id or "default", key)
