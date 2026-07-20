"""FastAPI surface for harness observability data.

Routes mounted under ``/api/harness``:

* ``GET /api/harness/metrics``      — tool-memo stats + tracer counters
                                       + boot uptime + inventory snapshot.
* ``GET /api/harness/trace``        — recent turn records (default 20).
* ``GET /api/harness/trace/tools``  — recent per-tool execution records.

All endpoints are read-only and never include core tool / skill names
in the inventory snapshot — they reuse :class:`HarnessInventory`'s
already-filtered output.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from .. import Harness


router = APIRouter(prefix="/api/harness", tags=["harness"])


def _get_harness(request: Request) -> Harness:
    harness = getattr(request.app.state, "harness", None)
    if harness is None:
        raise HTTPException(status_code=503, detail="harness not initialized")
    return harness


@router.get("/metrics")
async def harness_metrics(request: Request) -> dict[str, Any]:
    """Compact summary: inventory + memo stats + tracer counters."""
    harness = _get_harness(request)
    inv = harness.inventory()
    tool_memo = getattr(harness, "tool_memo", None)
    tracer = getattr(harness, "tracer", None)
    progress = getattr(harness, "progress", None)
    return {
        "uptime_seconds": tracer.uptime_seconds() if tracer is not None else None,
        "counts": {
            "user_skills": len(inv.skills),
            "user_mcps": len(inv.mcps),
            "user_plugins": len(inv.plugins),
            "user_tools": len(inv.tools),
        },
        "core_summary": dict(inv.core_summary),
        "tool_memo": {
            "enabled": tool_memo is not None,
            "memoizable": (
                sorted(tool_memo.memoizable_tools)
                if tool_memo is not None
                else []
            ),
            "stats": (
                tool_memo.stats.to_dict()
                if tool_memo is not None
                else {"hits": 0, "misses": 0, "evictions": 0, "skipped": 0}
            ),
        },
        "tracer": {
            "enabled": tracer is not None,
            "stats": (
                {
                    "turns_recorded": tracer.stats.turns_recorded,
                    "tools_recorded": tracer.stats.tools_recorded,
                }
                if tracer is not None
                else {"turns_recorded": 0, "tools_recorded": 0}
            ),
        },
        "progress": {
            "enabled": progress is not None,
            "default_cooldown_seconds": (
                progress.default_cooldown_seconds
                if progress is not None
                else 0.0
            ),
            "stats": (
                progress.stats.to_dict()
                if progress is not None
                else {
                    "tool_pings": 0, "phase_pings": 0,
                    "suppressed_dedup": 0, "suppressed_cooldown": 0,
                    "suppressed_no_sink": 0, "sink_errors": 0,
                }
            ),
        },
    }


@router.get("/trace")
async def harness_trace(
    request: Request,
    n: int = Query(20, ge=1, le=200),
) -> dict[str, Any]:
    """Recent ``run_turn`` records, oldest first within the window."""
    harness = _get_harness(request)
    tracer = getattr(harness, "tracer", None)
    if tracer is None:
        return {"enabled": False, "turns": []}
    return {
        "enabled": True,
        "turns": tracer.recent_turns(n),
    }


@router.get("/trace/tools")
async def harness_trace_tools(
    request: Request,
    n: int = Query(50, ge=1, le=500),
) -> dict[str, Any]:
    """Recent explicit harness tool-execution records."""
    harness = _get_harness(request)
    tracer = getattr(harness, "tracer", None)
    if tracer is None:
        return {"enabled": False, "tools": []}
    return {
        "enabled": True,
        "tools": tracer.recent_tools(n),
    }


__all__ = ["router"]
