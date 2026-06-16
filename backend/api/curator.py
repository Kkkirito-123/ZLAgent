"""REST surface for the v0.11 skill curator.

Three endpoints, all read-mostly:

* ``GET  /api/curator/state``    — last-run summary + service health
* ``POST /api/curator/run``      — trigger a review now (blocking)
* ``POST /api/curator/{name}/{action}`` — pin / unpin / mark_stale /
                                          mark_active / archive

Pin / archive go through the curator (which enforces the
``created_by="agent"`` invariant for archive). They are intentionally
*not* gated by the v0.6 IM confirmation flow because they are admin
actions invoked by the operator (or a future Web UI), not by the LLM.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/curator", tags=["curator"])

VALID_ACTIONS = ("pin", "unpin", "mark_stale", "mark_active", "archive")


class CuratorActionPayload(BaseModel):
    absorbed_into: Optional[str] = Field(
        default=None,
        description=(
            "Only used by the ``archive`` action. Records which skill the"
            " archived one was merged into (so the decision is auditable)."
        ),
    )


@router.get("/state")
def get_state(request: Request) -> dict[str, Any]:
    svc = getattr(request.app.state, "curator_service", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="curator service unavailable")
    return {
        "last_run_at": svc.last_run_at,
        "last_summary": svc.last_summary,
        "service_enabled": getattr(svc, "_enabled", False),
    }


@router.post("/run")
async def run_now(request: Request) -> dict[str, Any]:
    svc = getattr(request.app.state, "curator_service", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="curator service unavailable")
    summary = await svc.run_once()
    if summary is None:
        raise HTTPException(status_code=500, detail="curator review failed (see logs)")
    return {
        "ok": True,
        "last_run_at": svc.last_run_at,
        "summary": summary,
    }


@router.post("/{skill_name}/{action}")
def perform_action(
    skill_name: str,
    action: str,
    payload: CuratorActionPayload,
    request: Request,
) -> dict[str, Any]:
    if action not in VALID_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"unknown action {action!r}; expected one of {VALID_ACTIONS}",
        )
    curator = getattr(request.app.state, "skill_curator", None)
    if curator is None:
        raise HTTPException(status_code=503, detail="curator unavailable")

    if action == "pin":
        changed = curator.pin(skill_name, pinned=True)
    elif action == "unpin":
        changed = curator.pin(skill_name, pinned=False)
    elif action == "mark_stale":
        changed = curator.mark_stale(skill_name)
    elif action == "mark_active":
        changed = curator.mark_active(skill_name)
    else:  # archive
        changed = curator.archive(
            skill_name, absorbed_into=payload.absorbed_into,
        )
    return {"ok": True, "skill_name": skill_name, "action": action, "changed": changed}
