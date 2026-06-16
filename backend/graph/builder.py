from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Optional, TYPE_CHECKING

from .source import GraphSourceRecord

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from .llm_extractor import LLMGraphExtractor

_ENTITY_STOPWORDS = {
    "and",
    "or",
    "the",
    "a",
    "an",
    "of",
    "to",
    "in",
    "on",
    "for",
    "with",
    "by",
    "from",
    "is",
    "are",
    "as",
    "at",
    "this",
    "that",
    "these",
    "those",
    "这些",
    "一个",
    "以及",
    "和",
    "的",
}


@dataclass
class GraphNode:
    id: str
    kind: str
    label: str
    knowledge_base_id: str = "default"
    attrs: dict[str, object] = field(default_factory=dict)


@dataclass
class GraphEdge:
    source: str
    target: str
    kind: str
    attrs: dict[str, object] = field(default_factory=dict)


@dataclass
class GraphMindMapBranch:
    id: str
    label: str
    kind: str
    children: list["GraphMindMapBranch"] = field(default_factory=list)
    attrs: dict[str, object] = field(default_factory=dict)


@dataclass
class GraphSnapshot:
    generated_at: datetime
    nodes: list[GraphNode] = field(default_factory=list)
    edges: list[GraphEdge] = field(default_factory=list)
    mindmap: list[GraphMindMapBranch] = field(default_factory=list)
    review: dict[str, object] = field(default_factory=dict)


class GraphLLMReviewer:
    """Deterministic placeholder for a future LLM review stage."""

    def review(self, record: GraphSourceRecord) -> dict[str, object]:
        text = f"{record.title} {record.summary} {record.content}".lower()
        score = 0.3
        if any(token in text for token in ("paper", "论文", "arxiv", "abstract", "method", "dataset")):
            score = 0.92
        elif any(token in text for token in ("task", "工作流", "workflow", "plan", "todo")):
            score = 0.78
        elif any(token in text for token in ("runtime", "context", "上下文", "记忆", "memory")):
            score = 0.85
        elif any(token in text for token in ("policy", "mindmap", "graph", "图谱")):
            score = 0.88
        return {
            "approved": score >= 0.5,
            "score": round(score, 2),
            "category": self._category_for(record),
            "reason": "heuristic gate placeholder for later llm review",
        }

    def _category_for(self, record: GraphSourceRecord) -> str:
        mapping = {
            "paper": "paper",
            "task": "task",
            "runtime": "context",
            "manual": "policy",
            "memory": "memory",
        }
        return mapping.get(record.source_type, "topic")


class KnowledgeGraphBuilder:
    def __init__(
        self,
        reviewer: GraphLLMReviewer | None = None,
        *,
        extractor: Optional["LLMGraphExtractor"] = None,
        node_limit_per_kb: int = 80,
    ) -> None:
        self._reviewer = reviewer or GraphLLMReviewer()
        self._extractor = extractor
        self._node_limit_per_kb = max(10, int(node_limit_per_kb))

    def build_from_records(self, records: Iterable[GraphSourceRecord]) -> GraphSnapshot:
        snapshot = GraphSnapshot(generated_at=datetime.utcnow())
        nodes_by_id: dict[str, GraphNode] = {}
        mindmap_roots: dict[str, GraphMindMapBranch] = {}
        review_rows: list[dict[str, object]] = []
        label_to_node_id: dict[str, str] = {}
        canonical_to_node_id: dict[tuple[str, str], str] = {}
        edge_keys: set[tuple[str, str, str]] = set()
        llm_hit_count = 0

        for idx, record in enumerate(records, 1):
            review = self._reviewer.review(record)
            review_rows.append({"record_id": record.id, **review})
            if not review["approved"]:
                continue

            kb_id = record.knowledge_base_id or "default"
            source_node_id = f"{kb_id}:{record.source_type}:{idx}"
            source_node = GraphNode(
                id=source_node_id,
                kind=review["category"],
                label=record.title,
                knowledge_base_id=kb_id,
                attrs={
                    **record.attrs,
                    "source_type": record.source_type,
                    "review_score": review["score"],
                    "source_record_id": record.id,
                    "summary": record.summary,
                    "created_at": record.created_at,
                },
            )
            nodes_by_id[source_node.id] = source_node

            # v1.2 — if an LLM extraction is cached for this record, materialise
            # those typed nodes/edges and skip the heuristic entity scrape for
            # this record entirely. The heuristic still runs as a fallback for
            # records without a cache hit so the graph is never empty.
            if self._extractor is not None:
                cached = self._extractor.cached_for(record)
                if cached is not None and cached.nodes:
                    self._materialise_cached(
                        cached=cached,
                        record=record,
                        kb_id=kb_id,
                        source_node=source_node,
                        nodes_by_id=nodes_by_id,
                        edges=snapshot.edges,
                        edge_keys=edge_keys,
                    )
                    llm_hit_count += 1
                    continue

            root = mindmap_roots.setdefault(kb_id, GraphMindMapBranch(id=f"{kb_id}:root", label=kb_id, kind="root"))
            category_id = f"{kb_id}:{review['category']}"
            category_branch = next((child for child in root.children if child.id == category_id), None)
            if category_branch is None:
                category_branch = GraphMindMapBranch(
                    id=category_id,
                    label=str(review["category"]).title(),
                    kind=str(review["category"]),
                )
                root.children.append(category_branch)
            category_branch.children.append(
                GraphMindMapBranch(
                    id=record.id,
                    label=record.title,
                    kind=record.source_type,
                    attrs={
                        "summary": record.summary,
                        "score": review["score"],
                        "knowledge_base_id": kb_id,
                        **record.attrs,
                    },
                )
            )

            chunks = record.chunks or self._split_sentences(f"{record.summary}. {record.content}")
            chunk_ids = [self._chunk_id(record.id, idx) for idx, _ in enumerate(chunks, 1)]
            chunk_lookup = dict(zip(chunks, chunk_ids))
            entities = self._extract_entities(record)
            if not entities:
                entities = [record.title]
            entity_node_ids: list[str] = []
            record_evidence: list[str] = []
            for entity in entities:
                canonical = self._canonical_entity(entity)
                node_id = canonical_to_node_id.get((kb_id, canonical))
                if node_id is None:
                    node_id = self._entity_node_id(kb_id, canonical)
                    canonical_to_node_id[(kb_id, canonical)] = node_id
                    nodes_by_id[node_id] = GraphNode(
                        id=node_id,
                        kind="entity",
                        label=entity,
                        knowledge_base_id=kb_id,
                        attrs={
                            "canonical_name": canonical,
                            "aliases": [entity],
                            "source_record_ids": [record.id],
                            "evidence": [],
                            "source_type": record.source_type,
                            "entity_type": self._guess_entity_type(entity),
                        },
                    )
                node = nodes_by_id[node_id]
                aliases = node.attrs.setdefault("aliases", [])
                if entity not in aliases:
                    aliases.append(entity)
                source_record_ids = node.attrs.setdefault("source_record_ids", [])
                if record.id not in source_record_ids:
                    source_record_ids.append(record.id)
                evidence = node.attrs.setdefault("evidence", [])
                for chunk in chunks:
                    if entity.lower() in chunk.lower() and chunk not in evidence:
                        evidence.append(chunk[:240])
                        chunk_id = chunk_lookup.get(chunk)
                        if chunk_id and chunk_id not in record_evidence:
                            record_evidence.append(chunk_id)
                        if chunk not in record_evidence:
                            record_evidence.append(chunk[:240])
                if not evidence and record.summary and record.summary not in evidence:
                    evidence.append(record.summary)
                label_to_node_id.setdefault(entity.lower(), node_id)
                entity_node_ids.append(node_id)

            for entity_node_id in entity_node_ids:
                self._add_edge(
                    snapshot.edges,
                    edge_keys,
                    source_node.id,
                    entity_node_id,
                    "mentions_entity",
                    {
                        "knowledge_base_id": kb_id,
                        "source_record_id": record.id,
                        "evidence": record_evidence[0] if record_evidence else record.summary,
                        "source_chunk_ids": [cid for cid in record_evidence if cid.startswith("chunk:")],
                        "confidence": 0.55,
                    },
                )

            relation_pairs = self._extract_relations(record, entities, label_to_node_id, canonical_to_node_id, kb_id)
            for chunk in chunks:
                if len(record_evidence) >= 3:
                    break
                if chunk not in record_evidence:
                    record_evidence.append(chunk[:240])
            for source_id, target_id, relation_type, evidence, confidence in relation_pairs:
                self._add_edge(
                    snapshot.edges,
                    edge_keys,
                    source_id,
                    target_id,
                    relation_type,
                    {
                        "knowledge_base_id": kb_id,
                        "source_record_id": record.id,
                        "evidence": evidence,
                        "source_chunk_ids": self._match_chunk_ids(chunks, evidence),
                        "confidence": confidence,
                    },
                )

            # v1.2 — heuristic ``co_occurs_in_chunk`` edges removed: they
            # produced visual noise (entities that happened to share a
            # sentence) and made the canvas unreadable.  When the LLM
            # cache is populated the typed edges below carry the real
            # signal; the heuristic fallback keeps ``mentions_entity``
            # plus the regex-extracted relations.

        entity_counter = Counter()
        for node in nodes_by_id.values():
            if node.kind != "entity":
                continue
            canonical = str(node.attrs.get("canonical_name") or node.label)
            entity_counter[canonical] += len(node.attrs.get("source_record_ids", []))

        for kb_id, root in mindmap_roots.items():
            entity_branch = GraphMindMapBranch(id=f"{kb_id}:entities", label="Entities", kind="group")
            relation_branch = GraphMindMapBranch(id=f"{kb_id}:relations", label="Relations", kind="group")
            for node in nodes_by_id.values():
                if node.knowledge_base_id != kb_id or node.kind != "entity":
                    continue
                entity_branch.children.append(
                    GraphMindMapBranch(
                        id=node.id,
                        label=node.label,
                        kind=node.kind,
                        attrs={
                            "mentions": len(node.attrs.get("source_record_ids", [])),
                            "aliases": node.attrs.get("aliases", []),
                        },
                    )
                )
            for edge in snapshot.edges:
                if edge.attrs.get("knowledge_base_id") != kb_id or edge.kind == "mentions_entity":
                    continue
                relation_branch.children.append(
                    GraphMindMapBranch(
                        id=f"{edge.source}->{edge.target}:{edge.kind}",
                        label=edge.kind,
                        kind="relation",
                        attrs={
                            "evidence": edge.attrs.get("evidence"),
                            "confidence": edge.attrs.get("confidence"),
                        },
                    )
                )
            root.children = [entity_branch, relation_branch, *root.children]

        snapshot.nodes = list(nodes_by_id.values())
        snapshot.mindmap = list(mindmap_roots.values())
        entity_nodes = [node for node in nodes_by_id.values() if node.kind == "entity"]
        alias_count = sum(len(node.attrs.get("aliases", [])) for node in entity_nodes)
        provenance_edge_count = sum(1 for edge in snapshot.edges if edge.attrs.get("source_chunk_ids"))
        approved_count = sum(1 for row in review_rows if row["approved"])
        snapshot.review = {
            "reviewer": "llm+heuristic" if llm_hit_count else "heuristic-placeholder",
            "rows": review_rows,
            "approved_count": approved_count,
            "rejected_count": sum(1 for row in review_rows if not row["approved"]),
            "entity_count": len(entity_nodes),
            "relation_count": sum(1 for edge in snapshot.edges if edge.kind != "mentions_entity"),
            "alias_count": alias_count,
            "canonical_entity_count": len(entity_counter),
            "provenance_edge_count": provenance_edge_count,
            "top_entities": entity_counter.most_common(10),
            "llm_hit_count": llm_hit_count,
            "llm_miss_count": max(0, approved_count - llm_hit_count),
        }
        return snapshot

    def _materialise_cached(
        self,
        *,
        cached: object,  # CachedExtraction (avoids runtime import cycle)
        record: GraphSourceRecord,
        kb_id: str,
        source_node: GraphNode,
        nodes_by_id: dict[str, GraphNode],
        edges: list[GraphEdge],
        edge_keys: set[tuple[str, str, str]],
    ) -> None:
        """Materialise an LLMGraphCache entry into snapshot nodes/edges.

        ids are namespaced as ``{kb_id}:llm:{record.id}:{local_id}`` so
        nodes from different records never collide even when they share
        a label like "Transformer" — this keeps the cache schema simple
        (no global de-dupe needed) at the cost of per-record duplicates,
        which is fine because each record is its own paper.
        """
        record_prefix = f"{kb_id}:llm:{record.id}"
        local_to_global: dict[str, str] = {}
        cached_nodes = list(getattr(cached, "nodes", []) or [])
        cached_edges = list(getattr(cached, "edges", []) or [])
        for entry in cached_nodes:
            local_id = str(entry.get("id") or "").strip()
            kind = str(entry.get("kind") or "").strip()
            label = str(entry.get("label") or "").strip()
            if not local_id or not kind or not label:
                continue
            global_id = f"{record_prefix}:{local_id}"
            local_to_global[local_id] = global_id
            attrs = dict(entry.get("attrs") or {})
            attrs.setdefault("source_record_id", record.id)
            attrs.setdefault("llm_extracted", True)
            attrs.setdefault("schema", getattr(cached, "schema", ""))
            nodes_by_id[global_id] = GraphNode(
                id=global_id,
                kind=kind,
                label=label,
                knowledge_base_id=kb_id,
                attrs=attrs,
            )
            # Anchor the typed node to the record's source_node so the
            # source-card detail panel still resolves cleanly.
            self._add_edge(
                edges,
                edge_keys,
                source_node.id,
                global_id,
                "mentions_entity",
                {
                    "knowledge_base_id": kb_id,
                    "source_record_id": record.id,
                    "evidence": record.summary,
                    "confidence": 0.95,
                    "llm_extracted": True,
                },
            )
        for entry in cached_edges:
            src = local_to_global.get(str(entry.get("source") or ""))
            tgt = local_to_global.get(str(entry.get("target") or ""))
            kind = str(entry.get("kind") or "").strip()
            if not src or not tgt or not kind or src == tgt:
                continue
            attrs = dict(entry.get("attrs") or {})
            attrs.setdefault("knowledge_base_id", kb_id)
            attrs.setdefault("source_record_id", record.id)
            attrs.setdefault("llm_extracted", True)
            attrs.setdefault("confidence", 0.92)
            self._add_edge(edges, edge_keys, src, tgt, kind, attrs)

    @staticmethod
    def _canonical_entity(entity: str) -> str:
        text = entity.strip().lower()
        text = re.sub(r"[_\-]+", " ", text)
        text = re.sub(r"\s+", " ", text)
        text = re.sub(r"[^a-z0-9\u4e00-\u9fff ]+", "", text)
        return text.strip()

    @staticmethod
    def _entity_node_id(kb_id: str, entity: str) -> str:
        normalized = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", entity.lower()).strip("-")[:80] or "entity"
        return f"{kb_id}:entity:{normalized}"

    def _extract_entities(self, record: GraphSourceRecord) -> list[str]:
        candidates: list[str] = []
        candidates.extend(record.tags)
        candidates.extend(self._phrase_candidates(record.title))
        candidates.extend(self._phrase_candidates(record.summary))
        for sentence in self._split_sentences(record.content):
            candidates.extend(self._phrase_candidates(sentence))
        seen: set[str] = set()
        entities: list[str] = []
        for cand in candidates:
            norm = cand.strip(" ,.;:，。；：\"'()[]{}")
            if not norm or len(norm) < 2:
                continue
            if norm.lower() in _ENTITY_STOPWORDS:
                continue
            if any(stop in norm.lower() for stop in ("http", "www", "com")):
                continue
            canonical = self._canonical_entity(norm)
            if canonical in seen:
                continue
            seen.add(canonical)
            entities.append(norm[:80])
            if len(entities) >= 14:
                break
        return entities

    def _extract_relations(
        self,
        record: GraphSourceRecord,
        entities: list[str],
        label_to_node_id: dict[str, str],
        canonical_to_node_id: dict[tuple[str, str], str],
        kb_id: str,
    ) -> list[tuple[str, str, str, str, float]]:
        relations: list[tuple[str, str, str, str, float]] = []
        text = f"{record.title}. {record.summary}. {record.content}"
        for sentence in self._split_sentences(text):
            sentence_entities = self._entities_in_sentence(sentence, entities)
            direct_pairs = self._pairwise_relations(sentence, sentence_entities, label_to_node_id, canonical_to_node_id, kb_id)
            relations.extend(direct_pairs)

        if not relations and len(entities) >= 2:
            relations.append(
                (
                    self._entity_node_id(kb_id, self._canonical_entity(entities[0])),
                    self._entity_node_id(kb_id, self._canonical_entity(entities[1])),
                    "related_to",
                    self._best_evidence(record),
                    0.4,
                )
            )
        return relations

    def _pairwise_relations(
        self,
        sentence: str,
        sentence_entities: list[str],
        label_to_node_id: dict[str, str],
        canonical_to_node_id: dict[tuple[str, str], str],
        kb_id: str,
    ) -> list[tuple[str, str, str, str, float]]:
        if len(sentence_entities) < 2:
            return []
        relation_specs: list[tuple[re.Pattern[str], str, float]] = [
            (re.compile(r"(.+?)\s+(?:uses|utilizes|leverages|based on|built on)\s+(.+?)\b", re.IGNORECASE), "uses", 0.88),
            (re.compile(r"(.+?)\s+(?:improves?|boosts?|enhances?|optimizes?)\s+(.+?)\b", re.IGNORECASE), "improves", 0.9),
            (re.compile(r"(.+?)\s+(?:compares? with|versus|outperforms?|beats?)\s+(.+?)\b", re.IGNORECASE), "compares_with", 0.84),
            (re.compile(r"(.+?)\s+(?:depends on|requires|relies on)\s+(.+?)\b", re.IGNORECASE), "depends_on", 0.86),
            (re.compile(r"(.+?)\s+(?:contributes to|drives|supports|enables)\s+(.+?)\b", re.IGNORECASE), "supports", 0.82),
            (re.compile(r"(.+?)\s+(?:is a|is an|is the)\s+(.+?)\b", re.IGNORECASE), "is_a", 0.74),
            (re.compile(r"(.+?)\s+(?:contains|includes|consists of)\s+(.+?)\b", re.IGNORECASE), "contains", 0.8),
        ]
        relations: list[tuple[str, str, str, str, float]] = []
        for pattern, relation_type, confidence in relation_specs:
            for match in pattern.finditer(sentence):
                head = self._resolve_entity(match.group(1), sentence_entities, label_to_node_id, canonical_to_node_id, kb_id)
                tail = self._resolve_entity(match.group(2), sentence_entities, label_to_node_id, canonical_to_node_id, kb_id)
                if head and tail and head != tail:
                    relations.append((head, tail, relation_type, sentence[:240], confidence))
        if not relations and len(sentence_entities) >= 2:
            head = self._entity_node_id(kb_id, self._canonical_entity(sentence_entities[0]))
            tail = self._entity_node_id(kb_id, self._canonical_entity(sentence_entities[1]))
            relations.append((head, tail, "related_to", sentence[:240], 0.55))
        return relations

    def _best_evidence(self, record: GraphSourceRecord) -> str:
        if record.chunks:
            return record.chunks[0][:240]
        if record.summary:
            return record.summary[:240]
        return record.content[:240]

    @staticmethod
    def _chunk_id(record_id: str, index: int) -> str:
        return f"chunk:{record_id}:{index}"

    @staticmethod
    def _match_chunk_ids(chunks: list[str], evidence: str) -> list[str]:
        evidence_lower = evidence.lower()
        matched: list[str] = []
        for idx, chunk in enumerate(chunks, 1):
            if chunk.lower() in evidence_lower or evidence_lower in chunk.lower() or any(token in chunk.lower() for token in evidence_lower.split()[:3]):
                matched.append(f"chunk:{idx}")
        return matched

    @staticmethod
    def _resolve_entity(
        fragment: str,
        entities: list[str],
        label_to_node_id: dict[str, str],
        canonical_to_node_id: dict[tuple[str, str], str],
        kb_id: str,
    ) -> str | None:
        cleaned = fragment.strip().strip(" ,.;:，。；：\"'()[]{}")
        cleaned_lower = cleaned.lower()
        for entity in entities:
            entity_lower = entity.lower()
            if entity_lower in cleaned_lower or cleaned_lower in entity_lower:
                canonical = re.sub(r"[_\-]+", " ", entity_lower)
                canonical = re.sub(r"\s+", " ", canonical)
                canonical = re.sub(r"[^a-z0-9\u4e00-\u9fff ]+", "", canonical).strip()
                node_id = canonical_to_node_id.get((kb_id, canonical))
                if node_id:
                    return node_id
                fallback_slug = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", entity_lower).strip("-")[:80] or "entity"
                return f"{kb_id}:entity:{fallback_slug}"
        for label, node_id in label_to_node_id.items():
            if label in cleaned_lower:
                return node_id
        return None

    @staticmethod
    def _split_sentences(text: str) -> list[str]:
        parts = re.split(r"(?<=[。！？!?\.])\s+|\n+", text)
        return [part.strip() for part in parts if part and part.strip()]

    @staticmethod
    def _phrase_candidates(text: str) -> list[str]:
        tokens = re.findall(r"[A-Za-z][A-Za-z0-9_\-]{2,}|[\u4e00-\u9fff]{2,8}", text)
        return [token.replace("_", " ") for token in tokens]

    @staticmethod
    def _entities_in_sentence(sentence: str, entities: list[str]) -> list[str]:
        sentence_lower = sentence.lower()
        found: list[str] = []
        for entity in entities:
            if entity.lower() in sentence_lower:
                found.append(entity)
        return found

    @staticmethod
    def _guess_entity_type(entity: str) -> str:
        lowered = entity.lower()
        if any(token in lowered for token in ("model", "method", "algorithm", "framework", "pipeline")):
            return "method"
        if any(token in lowered for token in ("dataset", "benchmark", "corpus", "data")):
            return "dataset"
        if any(token in lowered for token in ("paper", "article", "memo", "note")):
            return "document"
        if any(token in lowered for token in ("agent", "runtime", "context", "memory")):
            return "system"
        if re.search(r"[\u4e00-\u9fff]", entity):
            return "topic"
        return "concept"

    @staticmethod
    def _add_edge(
        edges: list[GraphEdge],
        edge_keys: set[tuple[str, str, str]],
        source: str,
        target: str,
        kind: str,
        attrs: dict[str, object],
    ) -> None:
        key = (source, target, kind)
        if key in edge_keys:
            return
        edge_keys.add(key)
        edges.append(GraphEdge(source=source, target=target, kind=kind, attrs=attrs))
