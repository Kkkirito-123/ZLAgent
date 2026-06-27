"""REST surface for the v0.43 Phase B daily end-of-day review.

Mirrors :mod:`backend.api.curator`:

* ``GET  /api/review/state`` — last-run summary + service health.
* ``POST /api/review/run``   — fire a review pass right now (blocks
  until the LLM finishes; intended for operator debugging or a manual
  end-of-day kick when the cron-based 24h cadence isn't enough).
* ``GET  /api/review/log`` — most recent audit rows (one per run).
* ``POST /api/review/log/{run_id}/rollback`` — reverse memory effects
  of run ``run_id``; skill file changes are NOT auto-reverted (see
  :meth:`DailyReviewService.rollback_run` docstring for rationale).

v0.43 Phase B+ split the "run" and "log" concerns: ``state`` is
last-run convenience, ``log`` is the append-only audit trail. The
logs give the operator an emergency brake when the review fork
decides something they don't like.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

router = APIRouter(prefix="/api/review", tags=["review"])


class RollbackBody(BaseModel):
    """Optional payload for POST /log/{run_id}/rollback."""

    note: Optional[str] = None


@router.get("/state")
def get_state(request: Request) -> dict[str, Any]:
    svc = getattr(request.app.state, "daily_review_service", None)
    if svc is None:
        raise HTTPException(
            status_code=503, detail="daily review service unavailable",
        )
    return {
        "last_run_at": svc.last_run_at,
        "last_summary": svc.last_summary,
        "service_enabled": svc.enabled,
        "push_target_id": svc.push_target_id,
        "last_push_status": svc.last_push_status,
    }


@router.post("/run")
async def run_now(request: Request) -> dict[str, Any]:
    svc = getattr(request.app.state, "daily_review_service", None)
    if svc is None:
        raise HTTPException(
            status_code=503, detail="daily review service unavailable",
        )
    summary = await svc.run_once()
    if summary is None:
        # The service swallows internal errors and logs them. From the
        # operator's perspective a 500 with a clear pointer at the
        # logs is the most useful failure mode here.
        raise HTTPException(
            status_code=500,
            detail="daily review failed (see logs); service continues running",
        )
    return {
        "ok": True,
        "last_run_at": svc.last_run_at,
        "summary": summary,
    }


@router.get("/log")
def get_log(
    request: Request,
    limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, Any]:
    """Return recent review runs, newest first, each with rollback state."""
    svc = getattr(request.app.state, "daily_review_service", None)
    if svc is None or svc.action_log is None:
        raise HTTPException(
            status_code=503, detail="review audit log unavailable",
        )
    runs = svc.action_log.list_runs(limit=limit)
    return {"ok": True, "count": len(runs), "runs": runs}


@router.get("/log/{run_id}")
def get_log_entry(request: Request, run_id: int) -> dict[str, Any]:
    svc = getattr(request.app.state, "daily_review_service", None)
    if svc is None or svc.action_log is None:
        raise HTTPException(
            status_code=503, detail="review audit log unavailable",
        )
    row = svc.action_log.get_run(run_id)
    if row is None:
        raise HTTPException(
            status_code=404, detail=f"review run #{run_id} not found",
        )
    return {"ok": True, "run": row}


@router.post("/log/{run_id}/rollback")
def rollback_run(
    request: Request,
    run_id: int,
    body: Optional[RollbackBody] = None,
) -> dict[str, Any]:
    """Archive new memory rows + unarchive previously-archived ones.

    Idempotent: calling twice returns ``ok=False, reason="already
    rolled back"`` on the second attempt. Skill file changes are
    NOT auto-reverted — the JSON response surfaces the affected
    skill_history slice so the operator can ``git revert`` or use
    the existing ``/api/skills/{name}`` endpoints manually.
    """
    svc = getattr(request.app.state, "daily_review_service", None)
    if svc is None or svc.action_log is None:
        raise HTTPException(
            status_code=503, detail="review audit log unavailable",
        )
    note = body.note if body is not None else None
    result = svc.rollback_run(int(run_id), note=note)
    # 200 for both ok and idempotent-no-op; 404 only when the run
    # doesn't exist. Lets the UI distinguish via payload.ok.
    if not result.get("ok") and str(result.get("reason", "")).startswith(
        "run #"
    ) and "not found" in str(result.get("reason", "")):
        raise HTTPException(status_code=404, detail=result["reason"])
    return result
