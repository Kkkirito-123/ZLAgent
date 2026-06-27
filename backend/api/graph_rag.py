"""GraphRAG dashboard endpoint.

Builds a consolidated snapshot from memory, graph records, and runtime
state so the front-end can render a GraphRAG-style dashboard without
assembling everything client-side.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Request

from ..agent.context import GraphEdgeSnapshot, GraphNodeSnapshot, GraphRAGSnapshot
from ..core.config import get_settings
from ..core.runtime import build_core_runtime_overview
from ..graph.builder import KnowledgeGraphBuilder
from ..graph.source import (
    GraphSourceBundle,
    GraphSourceRecord,
    collect_graph_records,
    list_knowledge_modes,
)
from ..memory.store import MemoryStore

router = APIRouter(prefix="/api/graph-rag", tags=["graph-rag"])


def _series_item(label: str, value: Any, status: str = "ready") -> dict[str, Any]:
    return {"label": label, "value": value, "status": status}


@router.get("")
def graph_rag_overview(request: Request) -> dict[str, Any]:
    store: MemoryStore | None = getattr(request.app.state, "memory_store", None)
    runtime_overview = build_core_runtime_overview(app=request.app).to_dict()
    runtime_overview["extensions"]["graph_rag"] = {
        "summary": "GraphRAG snapshot not initialized",
        "knowledge_base_count": 0,
        "node_count": 0,
        "edge_count": 0,
        "selected_node_id": None,
    }

    settings = get_settings()
    workspace_dir = settings.workspace_dir
    memory_knowledge_bases = store.get_knowledge_bases() if store is not None else []
    mode_knowledge_bases = list_knowledge_modes(workspace_dir)
    knowledge_base_ids = list(
        dict.fromkeys(["default", *memory_knowledge_bases, *mode_knowledge_bases])
    )

    knowledge_bases: list[dict[str, Any]] = []
    all_nodes: list[GraphNodeSnapshot] = []
    all_edges: list[GraphEdgeSnapshot] = []
    selected_kb_id: str | None = None
    selected_node_id: str | None = None
    review_rows: list[dict[str, Any]] = []

    for knowledge_base_id in knowledge_base_ids:
        bundle = collect_graph_records(
            knowledge_base_id=knowledge_base_id, workspace_dir=workspace_dir
        )
        snapshot = KnowledgeGraphBuilder(
            extractor=getattr(request.app.state, "graph_extractor", None),
            node_limit_per_kb=settings.graph_node_limit_per_kb,
        ).build_from_records(bundle.records)
        node_items = [
            GraphNodeSnapshot(
                id=node.id,
                label=node.label,
                kind=node.kind,
                knowledge_base_id=node.knowledge_base_id,
                attrs=dict(node.attrs),
            )
            for node in snapshot.nodes
        ]
        edge_items = [
            GraphEdgeSnapshot(
                source=edge.source,
                target=edge.target,
                kind=edge.kind,
                attrs=dict(edge.attrs),
            )
            for edge in snapshot.edges
        ]
        review_rows.extend(list(snapshot.review.get("rows", [])))
        knowledge_bases.append(
            {
                "id": knowledge_base_id,
                "name": knowledge_base_id,
                "kind": (
                    "memory" if knowledge_base_id == "default"
                    else ("paper" if knowledge_base_id in mode_knowledge_bases else "memory")
                ),
                "summary": f"{knowledge_base_id} 的 GraphRAG 子图快照",
                "status": "active",
                "nodes": [asdict(node) for node in node_items],
                "edges": [asdict(edge) for edge in edge_items],
                "template": "GraphRAG 总模板",
                "count": len(node_items),
                "mindmap": [asdict(branch) for branch in snapshot.mindmap],
                "review": snapshot.review,
                "sourceSummary": bundle.summary,
                "pendingReview": [asdict(record) for record in bundle.pending_review or []],
            }
        )
        if selected_kb_id is None:
            selected_kb_id = knowledge_base_id
            selected_node_id = node_items[0].id if node_items else None
        all_nodes.extend(node_items)
        all_edges.extend(edge_items)

    workflow = [
        _series_item("Document ingestion", len(all_nodes), "ready"),
        _series_item("Entity / relation extraction", len(all_edges), "ready"),
        _series_item("LLM review / gate", len(review_rows), "ready"),
        _series_item("Mind map generation", len(knowledge_bases), "ready"),
        _series_item("Graph retrieval", len(all_nodes) + len(all_edges), "ready"),
        _series_item("Response generation", runtime_overview["core"].get("llm_configured", False), "ready"),
    ]

    evidence = []
    for kb in knowledge_bases:
        if not kb["nodes"]:
            continue
        first = kb["nodes"][0]
        evidence.append(
            {
                "knowledgeBaseId": kb["id"],
                "nodeId": first["id"],
                "label": first["label"],
                "kind": first["kind"],
                "aliasCount": len(first.get("attrs", {}).get("aliases", [])),
                "sourceChunkIds": first.get("attrs", {}).get("source_chunk_ids", []),
            }
        )

    graph_snapshot = GraphRAGSnapshot(
        current_knowledge_base_id=selected_kb_id or "default",
        knowledge_base_count=len(knowledge_bases),
        node_count=len(all_nodes),
        edge_count=len(all_edges),
        nodes=all_nodes,
        edges=all_edges,
        selected_node_id=selected_node_id,
        summary=f"{len(knowledge_bases)} knowledge bases, {len(all_nodes)} nodes, {len(all_edges)} edges",
    )

    runtime_overview["extensions"]["graph_rag"] = {
        "summary": graph_snapshot.summary,
        "knowledge_base_count": graph_snapshot.knowledge_base_count,
        "node_count": graph_snapshot.node_count,
        "edge_count": graph_snapshot.edge_count,
        "selected_node_id": graph_snapshot.selected_node_id,
        "reviewed_items": len(review_rows),
        "provenance_edges": sum(1 for edge in all_edges if edge.attrs.get("source_chunk_ids")),
    }

    return {
        "currentKnowledgeBaseId": graph_snapshot.current_knowledge_base_id,
        "selectedNodeId": graph_snapshot.selected_node_id,
        "knowledgeBases": knowledge_bases,
        "workflow": workflow,
        "evidence": evidence,
        "runtime": runtime_overview,
    }


__all__ = ["router"]
