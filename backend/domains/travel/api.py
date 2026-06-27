"""Travel domain — REST endpoints for the visited-map view.

Extracted from ``backend/api/wiki.py`` so the wiki/ package stays
domain-agnostic. The router mounts under ``/api/wiki`` (same prefix
as the generic wiki router) so existing URLs keep working.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse


router = APIRouter(prefix="/api/wiki", tags=["wiki"])


def _store(request: Request):
    store = getattr(request.app.state, "wiki_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="wiki store not initialised")
    return store


def _geo_store(request: Request):
    store = getattr(request.app.state, "geo_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="geo store not initialised")
    return store


def _build_visited_map(request: Request, *, since_days: Optional[int], red_threshold: int):
    from .visited_map import VisitedMapBuilder
    builder = VisitedMapBuilder(
        _store(request),
        _geo_store(request),
        red_threshold=red_threshold,
    )
    return builder, builder.build(since_days=since_days)


@router.get("/visited-map", response_class=HTMLResponse)
def visited_map_html(
    request: Request,
    since_days: Optional[int] = Query(
        None, ge=1, le=3650,
        description="Only include facts from the last N days. Omit for all time.",
    ),
    red_threshold: int = Query(
        10, ge=2, le=10_000,
        description="Edge hit count that turns an edge (and endpoints) red.",
    ),
) -> HTMLResponse:
    """Standalone echarts page showing the 'been there' graph."""
    builder, data = _build_visited_map(
        request, since_days=since_days, red_threshold=red_threshold,
    )
    html = builder.render_html(data)
    return HTMLResponse(content=html)


@router.get("/visited-map.json")
def visited_map_json(
    request: Request,
    since_days: Optional[int] = Query(None, ge=1, le=3650),
    red_threshold: int = Query(10, ge=2, le=10_000),
) -> dict:
    """Same data as :func:`visited_map_html` but as raw JSON."""
    _, data = _build_visited_map(
        request, since_days=since_days, red_threshold=red_threshold,
    )
    return data
