"""Operator REST surface for v0.12 cross-session memory.

The IM-facing path is the ``memory_manage`` tool, which gates every write
through the v0.6 confirmation flow. This module is the **operator** path:
direct CRUD on the ``user_memories`` table from a Web Ops Panel or curl,
no LLM in the loop. That means we can offer a couple of operations the
LLM is intentionally denied — most notably ``force=true`` removal of
pinned entries and unrestricted forget.

Endpoints:

* ``GET    /api/memory``                 — list (with kind / archived filter)
* ``POST   /api/memory``                 — add (operator-blessed import)
* ``GET    /api/memory/stats``           — quick counts
* ``POST   /api/memory/consolidate``     — v0.15 deterministic merge sweep
* ``GET    /api/memory/{id}``            — read one
* ``DELETE /api/memory/{id}?force=...``  — remove (force overrides pin)
* ``POST   /api/memory/{id}/pin``        — set pinned=True
* ``POST   /api/memory/{id}/unpin``      — set pinned=False
* ``POST   /api/memory/{id}/archive``    — soft archive
* ``POST   /api/memory/{id}/unarchive``  — restore
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from ..memory.scanner import scan_content
from ..memory.store import (
    SOURCE_IMPORT,
    VALID_KINDS,
    MemoryError,
    MemoryStore,
)

router = APIRouter(prefix="/api/memory", tags=["memory"])


def _store(request: Request) -> MemoryStore:
    store = getattr(request.app.state, "memory_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="memory store not initialised")
    return store


# -----------------------------------------------------------------------------
# Pydantic shapes
# -----------------------------------------------------------------------------


class MemoryIn(BaseModel):
    kind: str = Field(default="user_fact", description="user_fact | agent_note")
    content: str = Field(min_length=1, max_length=2000)
    pinned: bool = False
    knowledge_base_id: str = Field(default="default")


class MemoryOut(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: int
    kind: str
    content: str
    source: str
    pinned: bool
    archived: bool
    recall_count: int
    last_recalled_at: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------


@router.get("", response_model=list[MemoryOut])
def list_memories(
    request: Request,
    kind: Optional[str] = Query(None, description="user_fact | agent_note"),
    include_archived: bool = Query(False),
    limit: Optional[int] = Query(None, ge=1, le=500),
) -> list[dict[str, Any]]:
    if kind is not None and kind not in VALID_KINDS:
        raise HTTPException(
            status_code=400,
            detail=f"invalid kind {kind!r}; expected one of {sorted(VALID_KINDS)}",
        )
    try:
        return _store(request).list(
            kind=kind, include_archived=include_archived, limit=limit,
        )
    except MemoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("", response_model=MemoryOut, status_code=201)
def add_memory(payload: MemoryIn, request: Request) -> dict[str, Any]:
    if payload.kind not in VALID_KINDS:
        raise HTTPException(
            status_code=400,
            detail=f"invalid kind {payload.kind!r}; expected one of {sorted(VALID_KINDS)}",
        )
    veto = scan_content(payload.content)
    if veto is not None:
        # Operator-driven write still goes through the scanner — same
        # threat model regardless of the entry point.
        raise HTTPException(status_code=400, detail=veto)
    try:
        return _store(request).add(
            payload.content,
            kind=payload.kind,
            source=SOURCE_IMPORT,
            pinned=payload.pinned,
            knowledge_base_id=payload.knowledge_base_id,
        )
    except MemoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/stats")
def stats(request: Request, knowledge_base_id: Optional[str] = Query(None)) -> dict[str, Any]:
    return _store(request).stats(knowledge_base_id=knowledge_base_id)


class ConsolidateIn(BaseModel):
    kind: Optional[str] = Field(
        default=None, description="user_fact | agent_note (None = both)",
    )
    similarity_threshold: float = Field(
        default=0.80, ge=0.5, le=1.0,
        description="SequenceMatcher ratio cutoff. 0.80 by default.",
    )


@router.post("/consolidate")
def consolidate_memories(payload: ConsolidateIn, request: Request) -> dict[str, Any]:
    """Deterministic merge sweep over near-duplicate active entries.

    Mirrors the IM-facing ``memory_manage(action='consolidate')`` flow,
    only without the confirmation step. Operator-only.
    """
    if payload.kind is not None and payload.kind not in VALID_KINDS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"invalid kind {payload.kind!r}; expected one of {sorted(VALID_KINDS)}"
            ),
        )
    try:
        return _store(request).consolidate(
            similarity_threshold=payload.similarity_threshold,
            kind=payload.kind,
        )
    except MemoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{memory_id}", response_model=MemoryOut)
def get_memory(memory_id: int, request: Request) -> dict[str, Any]:
    row = _store(request).get(memory_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"memory #{memory_id} not found")
    return row


@router.delete("/{memory_id}", status_code=204)
def delete_memory(
    memory_id: int,
    request: Request,
    force: bool = Query(
        False, description="Override pin protection. Operator-only.",
    ),
) -> None:
    try:
        removed = _store(request).remove(memory_id, allow_pinned=force)
    except MemoryError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not removed:
        raise HTTPException(status_code=404, detail=f"memory #{memory_id} not found")


@router.post("/{memory_id}/pin")
def pin_memory(memory_id: int, request: Request) -> dict[str, Any]:
    if not _store(request).set_pinned(memory_id, True):
        raise HTTPException(status_code=404, detail=f"memory #{memory_id} not found")
    return {"ok": True, "memory_id": memory_id, "pinned": True}


@router.post("/{memory_id}/unpin")
def unpin_memory(memory_id: int, request: Request) -> dict[str, Any]:
    if not _store(request).set_pinned(memory_id, False):
        raise HTTPException(status_code=404, detail=f"memory #{memory_id} not found")
    return {"ok": True, "memory_id": memory_id, "pinned": False}


@router.post("/{memory_id}/archive")
def archive_memory(memory_id: int, request: Request) -> dict[str, Any]:
    if not _store(request).archive(memory_id):
        raise HTTPException(status_code=404, detail=f"memory #{memory_id} not found")
    return {"ok": True, "memory_id": memory_id, "archived": True}


@router.post("/{memory_id}/unarchive")
def unarchive_memory(memory_id: int, request: Request) -> dict[str, Any]:
    if not _store(request).unarchive(memory_id):
        raise HTTPException(status_code=404, detail=f"memory #{memory_id} not found")
    return {"ok": True, "memory_id": memory_id, "archived": False}
