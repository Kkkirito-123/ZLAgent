"""Read-only skills listing endpoint for v0.1, plus the v0.15 history log surface."""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from ..core.config import get_settings
from ..skills.loader import SkillLoader

router = APIRouter(prefix="/api/skills", tags=["skills"])


@router.get("")
async def list_skills() -> dict[str, Any]:
    settings = get_settings()
    loader = SkillLoader(settings.workspace_dir / "skills")
    loader.load()
    return {
        "count": len(loader.list()),
        "items": [
            {
                "id": manifest.id,
                "name": manifest.name,
                "description": manifest.description,
                "created_by": manifest.created_by,
                "tags": manifest.tags,
                "version": manifest.version,
            }
            for manifest in loader.list()
        ],
    }


@router.get("/history")
async def list_history(
    request: Request,
    skill_name: Optional[str] = Query(default=None, description="Filter by skill name."),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, Any]:
    """Return recent skill mutations from the v0.15 diff log.

    Filtered, newest-first. Each entry includes the unified-diff snippet
    (capped at 4 KiB inside the store), so an operator can read the diff
    log straight in the browser / Web Ops panel.
    """
    store = getattr(request.app.state, "skill_history_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="skill history not initialized")
    entries = store.list_entries(skill_name=skill_name, limit=limit)
    return {
        "count_returned": len(entries),
        "entries": entries,
        "stats": store.stats(),
    }
