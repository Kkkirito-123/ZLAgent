"""Smoke test for LLM graph extractor + cache + builder integration.

Run: ``python scripts/smoke_llm_graph.py``  (exit 0 on success).

This test stubs the LLM via ``chat_fn`` so it doesn't need an API key.
It covers:

* ``LLMGraphCache`` atomic write / readback / corrupt-file fail-open.
* ``LLMGraphExtractor.extract`` for the paper & memory schemas, plus
  timeout / non-JSON / fenced-JSON tolerance.
* ``KnowledgeGraphBuilder`` materialises cached LLM nodes/edges as
  proper ``Paper/Keyword/Author`` graph nodes (not heuristic entities).
* Suppression of ``co_occurs_in_chunk`` edges (the noise the user
  complained about in the screenshot).
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.graph.builder import KnowledgeGraphBuilder  # noqa: E402
from backend.graph.cache import (  # noqa: E402
    CachedExtraction,
    LLMGraphCache,
    record_cache_key,
)
from backend.graph.llm_extractor import LLMGraphExtractor  # noqa: E402
from backend.graph.source import GraphSourceRecord  # noqa: E402


def _record(*, kb: str = "ai-paper", source_type: str = "paper") -> GraphSourceRecord:
    return GraphSourceRecord(
        id=f"mode:{kb}:attention",
        source_type=source_type,
        knowledge_base_id=kb,
        title="Attention Is All You Need",
        summary="Transformer 引入纯注意力机制取代 RNN/CNN。",
        content="The paper proposes the Transformer, relying entirely on attention. Authored by Vaswani et al. Published at NeurIPS 2017.",
        tags=["transformer", "attention"],
        attrs={"source_url": "https://arxiv.org/abs/1706.03762"},
        chunks=["The paper proposes the Transformer."],
        created_at="2017-06-12",
    )


def test_cache_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cache = LLMGraphCache(Path(tmp))
        key = record_cache_key("r1", "hello world", max_chars=100)
        assert cache.get("kb1", key) is None
        cache.put(
            "kb1",
            key,
            CachedExtraction(
                nodes=[{"id": "paper", "kind": "Paper", "label": "X", "attrs": {}}],
                edges=[],
                schema="paper",
                generated_at=LLMGraphCache.now_iso(),
            ),
        )
        got = cache.get("kb1", key)
        assert got is not None and got.nodes[0]["label"] == "X"
        # Corrupt file → fail-open empty
        path = Path(tmp) / "kb1.json"
        path.write_text("{ this is not json", encoding="utf-8")
        assert cache.get("kb1", key) is None
    print("[ok] cache roundtrip + corrupt fail-open")


async def test_extractor_paper() -> None:
    paper_json = {
        "nodes": [
            {"id": "paper", "kind": "Paper", "label": "Attention Is All You Need",
             "attrs": {"title": "Attention Is All You Need", "source_url": "https://arxiv.org/abs/1706.03762", "summary": "transformer", "year": "2017"}},
            {"id": "transformer", "kind": "Keyword", "label": "Transformer", "attrs": {"kind": "topic"}},
            {"id": "attention", "kind": "Keyword", "label": "Attention", "attrs": {"kind": "topic"}},
            {"id": "vaswani", "kind": "Author", "label": "Vaswani", "attrs": {}},
            {"id": "neurips", "kind": "Venue", "label": "NeurIPS 2017", "attrs": {}},
            {"id": "junk", "kind": "NotAValidKind", "label": "drop me", "attrs": {}},
        ],
        "edges": [
            {"source": "paper", "target": "transformer", "kind": "has_keyword"},
            {"source": "paper", "target": "attention", "kind": "has_keyword"},
            {"source": "paper", "target": "vaswani", "kind": "authored_by"},
            {"source": "paper", "target": "neurips", "kind": "published_at"},
            {"source": "paper", "target": "missing", "kind": "has_keyword"},  # drop: target missing
        ],
    }
    # Wrap in fenced JSON to exercise the tolerant parser.
    fenced = "Here's the JSON:\n```json\n" + json.dumps(paper_json) + "\n```"

    async def chat_fn(_messages):
        return fenced

    with tempfile.TemporaryDirectory() as tmp:
        cache = LLMGraphCache(Path(tmp))
        ex = LLMGraphExtractor(llm=None, cache=cache, chat_fn=chat_fn)
        result = await ex.extract_and_cache(_record())
        assert result is not None
        kinds = sorted({n.kind for n in result.nodes})
        assert kinds == ["Author", "Keyword", "Paper", "Venue"], kinds
        edge_kinds = sorted({e.kind for e in result.edges})
        assert edge_kinds == ["authored_by", "has_keyword", "published_at"], edge_kinds
        assert len(result.edges) == 4  # missing-target edge dropped
        # Cache hit on retry
        cached = ex.cached_for(_record())
        assert cached is not None and len(cached.nodes) == 5
    print("[ok] extractor paper schema + fenced JSON + cache write")


async def test_extractor_memory() -> None:
    mem_json = {
        "nodes": [
            {"id": "memory", "kind": "Memory", "label": "用户偏好简短回复",
             "attrs": {"summary": "用户喜欢简短回复", "kind": "user_fact"}},
            {"id": "short-reply", "kind": "Preference", "label": "简短回复 ≤50字", "attrs": {}},
        ],
        "edges": [{"source": "memory", "target": "short-reply", "kind": "prefers"}],
    }

    async def chat_fn(_messages):
        return json.dumps(mem_json)

    rec = GraphSourceRecord(
        id="memory:1", source_type="memory", knowledge_base_id="default",
        title="用户偏好", summary="喜欢简短回复", content="用户表示希望回复简短",
        tags=[], attrs={}, chunks=[], created_at=None,
    )
    with tempfile.TemporaryDirectory() as tmp:
        cache = LLMGraphCache(Path(tmp))
        ex = LLMGraphExtractor(llm=None, cache=cache, chat_fn=chat_fn)
        result = await ex.extract_and_cache(rec)
        assert result is not None and result.schema == "memory"
        assert any(n.kind == "Memory" for n in result.nodes)
        assert any(n.kind == "Preference" for n in result.nodes)
        assert result.edges[0].kind == "prefers"
    print("[ok] extractor memory schema")


async def test_extractor_failures() -> None:
    async def garbage(_m):
        return "I refuse to output JSON, sorry."

    async def slow(_m):
        await asyncio.sleep(2.0)
        return "{}"

    async def boom(_m):
        raise RuntimeError("upstream 502")

    with tempfile.TemporaryDirectory() as tmp:
        cache = LLMGraphCache(Path(tmp))
        rec = _record()
        for chat in (garbage, boom):
            ex = LLMGraphExtractor(llm=None, cache=cache, chat_fn=chat)
            result = await ex.extract(rec)
            assert result is None
        ex_slow = LLMGraphExtractor(llm=None, cache=cache, chat_fn=slow, timeout_seconds=0.2)
        result = await ex_slow.extract(rec)
        assert result is None
        # Cache should remain empty
        assert cache.get(rec.knowledge_base_id, record_cache_key(rec.id, rec.content)) is None
    print("[ok] extractor fail-soft on bad JSON / exception / timeout")


async def test_builder_uses_cached_extraction() -> None:
    paper_json = {
        "nodes": [
            {"id": "paper", "kind": "Paper", "label": "Attention Is All You Need",
             "attrs": {"title": "Attention Is All You Need", "source_url": "https://arxiv.org/abs/1706.03762"}},
            {"id": "transformer", "kind": "Keyword", "label": "Transformer", "attrs": {}},
        ],
        "edges": [{"source": "paper", "target": "transformer", "kind": "has_keyword"}],
    }

    async def chat_fn(_m):
        return json.dumps(paper_json)

    with tempfile.TemporaryDirectory() as tmp:
        cache = LLMGraphCache(Path(tmp))
        ex = LLMGraphExtractor(llm=None, cache=cache, chat_fn=chat_fn)
        rec = _record()
        await ex.extract_and_cache(rec)

        builder = KnowledgeGraphBuilder(extractor=ex)
        snapshot = builder.build_from_records([rec])

        kinds = sorted({n.kind for n in snapshot.nodes})
        assert "Paper" in kinds and "Keyword" in kinds, kinds
        # No heuristic entity nodes should have been generated for this record
        assert not any(n.kind == "entity" for n in snapshot.nodes), [n.kind for n in snapshot.nodes]
        # No co_occurs_in_chunk edges at all
        assert all(e.kind != "co_occurs_in_chunk" for e in snapshot.edges)
        # has_keyword edge present
        assert any(e.kind == "has_keyword" for e in snapshot.edges)
        assert snapshot.review["llm_hit_count"] == 1
        assert snapshot.review["reviewer"] == "llm+heuristic"
    print("[ok] builder materialises cached LLM extraction")


def test_builder_heuristic_fallback() -> None:
    rec = _record()
    snapshot = KnowledgeGraphBuilder().build_from_records([rec])
    # Without extractor, builder falls back to heuristic — should still produce
    # some nodes and edges but no co_occurs_in_chunk noise.
    assert snapshot.nodes
    assert all(e.kind != "co_occurs_in_chunk" for e in snapshot.edges)
    assert snapshot.review["llm_hit_count"] == 0
    print("[ok] builder heuristic fallback drops co_occurs_in_chunk noise")


async def _amain() -> int:
    test_cache_roundtrip()
    await test_extractor_paper()
    await test_extractor_memory()
    await test_extractor_failures()
    await test_builder_uses_cached_extraction()
    test_builder_heuristic_fallback()
    print("\nSMOKE PASS — LLM graph extraction wired end-to-end")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_amain()))
