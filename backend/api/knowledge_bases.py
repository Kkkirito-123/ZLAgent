"""Operator surface for knowledge-base listing, graph export, and paper imports.

This API keeps the model explicit: a knowledge base is a namespace. The
operator can list namespaces, inspect their current graph snapshot, and
ingest paper-like records into a chosen namespace.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from loguru import logger
from pydantic import BaseModel, Field

from ..core.config import get_settings
from ..graph.builder import KnowledgeGraphBuilder
from ..graph.llm_extractor import LLMGraphExtractor
from ..graph.source import collect_graph_records, list_knowledge_modes
from ..memory.store import MemoryStore

router = APIRouter(prefix="/api/knowledge-bases", tags=["knowledge-bases"])


class PaperIn(BaseModel):
    title: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)
    url: Optional[str] = None


class PaperImportIn(BaseModel):
    papers: list[PaperIn] = Field(default_factory=list)
    de_dupe: bool = True


@router.get("")
def list_knowledge_bases(request: Request) -> dict[str, Any]:
    store: MemoryStore | None = getattr(request.app.state, "memory_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="memory store not initialised")
    settings = get_settings()
    workspace_dir = settings.workspace_dir
    memory_bases = store.get_knowledge_bases()
    mode_bases = list_knowledge_modes(workspace_dir)
    # Preserve order: default first, then memory namespaces, then filesystem modes.
    bases: list[str] = ["default"]
    for kb_id in [*memory_bases, *mode_bases]:
        if kb_id and kb_id not in bases:
            bases.append(kb_id)
    extractor: Optional[LLMGraphExtractor] = getattr(request.app.state, "graph_extractor", None)
    items = []
    for kb_id in bases:
        records = collect_graph_records(
            knowledge_base_id=kb_id, workspace_dir=workspace_dir
        )
        snapshot = KnowledgeGraphBuilder(
            extractor=extractor,
            node_limit_per_kb=settings.graph_node_limit_per_kb,
        ).build_from_records(records.records)
        items.append({
            "id": kb_id,
            "name": kb_id,
            "kind": "memory" if kb_id == "default" else ("paper" if kb_id in mode_bases else "memory"),
            "summary": f"{kb_id} 的独立知识库",
            "status": "active",
            "count": len(snapshot.nodes),
            "template": "GraphRAG 总模板",
            "mindmap": [asdict(branch) for branch in snapshot.mindmap],
            "review": snapshot.review,
        })
    return {"count": len(items), "items": items}


@router.get("/{knowledge_base_id}/graph")
def get_graph(
    knowledge_base_id: str,
    request: Request,
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    settings = get_settings()
    bundle = collect_graph_records(
        knowledge_base_id=knowledge_base_id, workspace_dir=settings.workspace_dir
    )
    records = bundle.records[:limit]
    extractor: Optional[LLMGraphExtractor] = getattr(request.app.state, "graph_extractor", None)
    snapshot = KnowledgeGraphBuilder(
        extractor=extractor,
        node_limit_per_kb=settings.graph_node_limit_per_kb,
    ).build_from_records(records)
    return {
        "knowledge_base_id": knowledge_base_id,
        "generated_at": snapshot.generated_at.isoformat(),
        "nodes": [asdict(node) for node in snapshot.nodes],
        "edges": [asdict(edge) for edge in snapshot.edges],
        "review": snapshot.review,
    }


@router.post("/{knowledge_base_id}/rebuild-graph")
async def rebuild_graph(
    knowledge_base_id: str,
    request: Request,
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    """Force LLM re-extraction for every record in a KB and refresh the cache.

    Wall-clock capped at 60s; remaining records can be picked up by a
    follow-up call.  Returns counts so callers can show progress.
    """
    extractor: Optional[LLMGraphExtractor] = getattr(request.app.state, "graph_extractor", None)
    if extractor is None or not extractor.configured:
        raise HTTPException(
            status_code=503,
            detail="graph extractor unavailable — set graph_llm_enabled=true and configure the LLM",
        )
    settings = get_settings()
    bundle = collect_graph_records(
        knowledge_base_id=knowledge_base_id, workspace_dir=settings.workspace_dir
    )
    records = bundle.records[:limit]
    rebuilt = 0
    failed = 0
    skipped = 0
    deadline = asyncio.get_event_loop().time() + 60.0
    for record in records:
        if asyncio.get_event_loop().time() > deadline:
            skipped = len(records) - rebuilt - failed
            break
        try:
            result = await extractor.extract_and_cache(record)
        except Exception as exc:  # noqa: BLE001 — fail-soft per record
            logger.warning("[rebuild_graph] {} -> {}: {}", knowledge_base_id, record.id, exc)
            failed += 1
            continue
        if result is None or not result.nodes:
            failed += 1
        else:
            rebuilt += 1
    return {
        "ok": True,
        "knowledge_base_id": knowledge_base_id,
        "total": len(records),
        "rebuilt": rebuilt,
        "failed": failed,
        "skipped": skipped,
    }


@router.post("/{knowledge_base_id}/papers/import")
def import_papers(
    knowledge_base_id: str,
    payload: PaperImportIn,
    request: Request,
) -> dict[str, Any]:
    store: MemoryStore | None = getattr(request.app.state, "memory_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="memory store not initialised")

    imported = []
    deduped = 0
    for paper in payload.papers:
        content = f"{paper.title}\n{paper.summary}"
        existing = store.search(
            paper.title,
            knowledge_base_id=knowledge_base_id,
            limit=20,
        )
        if payload.de_dupe and any((paper.title or "").strip().lower() == (row.get("content") or "").split("\n", 1)[0].strip().lower() for row in existing):
            deduped += 1
            continue
        row = store.add(
            content,
            kind="agent_note",
            source="import",
            knowledge_base_id=knowledge_base_id,
        )
        imported.append(row)

    return {
        "ok": True,
        "knowledge_base_id": knowledge_base_id,
        "imported": len(imported),
        "deduped": deduped,
        "items": imported,
    }
