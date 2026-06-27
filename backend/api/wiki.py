"""REST surface for the wiki cache.

Three endpoints, all read-only or admin:

* ``GET    /api/wiki``          — list cached entries (filterable by skill).
* ``DELETE /api/wiki/{id}``     — invalidate a single entry (e.g. a bad
                                  answer the operator wants gone).
* ``POST   /api/wiki/refresh``  — sweep expired rows, returns the count.

The agent loop is the only writer in normal operation — entries appear
because real users asked questions that the LLM answered. There is
deliberately no ``POST`` to add an entry by hand; if an operator wants
to seed an answer they should drive a real turn through the gateway
test endpoint, which goes through the same path and applies the same
guardrails.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from ..wiki.store import WikiStore

router = APIRouter(prefix="/api/wiki", tags=["wiki"])


def _store(request: Request) -> WikiStore:
    store = getattr(request.app.state, "wiki_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="wiki store not initialised")
    return store


# -----------------------------------------------------------------------------
# Pydantic shapes
# -----------------------------------------------------------------------------


class WikiEntryOut(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: int
    skill_id: str
    raw_query: str
    normalized_query: str
    answer: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[str] = None
    last_hit_at: Optional[str] = None
    hit_count: int
    expires_at: Optional[str] = None


class WikiListOut(BaseModel):
    """Wrapper so future fields (totals, has_next) can be added without
    breaking clients that consume the response shape."""

    items: list[WikiEntryOut]
    count: int


class WikiRefreshOut(BaseModel):
    removed: int


# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------


@router.get("", response_model=WikiListOut)
def list_entries(
    request: Request,
    skill_id: Optional[str] = Query(
        None,
        description="Filter to one skill. Omit to list across every skill.",
    ),
    limit: int = Query(100, ge=1, le=500),
) -> WikiListOut:
    rows = _store(request).list_entries(skill_id=skill_id, limit=limit)
    items = [WikiEntryOut(**row) for row in rows]
    return WikiListOut(items=items, count=len(items))


@router.delete("/{entry_id}", status_code=204)
def delete_entry(entry_id: int, request: Request) -> None:
    removed = _store(request).delete(entry_id)
    if not removed:
        raise HTTPException(status_code=404, detail=f"wiki entry {entry_id} not found")
    return None


@router.post("/refresh", response_model=WikiRefreshOut)
def refresh(request: Request) -> WikiRefreshOut:
    """Drop every entry whose ``expires_at <= now``.

    Returns the row count for operator confirmation. Idempotent:
    calling twice in a row removes 0 the second time. Timeless rows
    (``expires_at IS NULL``) are not touched.
    """
    removed = _store(request).expire_stale()
    return WikiRefreshOut(removed=removed)


