from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api/maintenance", tags=["maintenance"])


@router.post("/run")
async def run_maintenance(request: Request) -> dict:
    maintenance = getattr(request.app.state, "memory_maintenance", None)
    if maintenance is None:
        raise HTTPException(status_code=500, detail="memory maintenance not initialized")
    result = maintenance.run()
    return result
