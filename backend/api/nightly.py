from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api/nightly", tags=["nightly"])


@router.post("/graph/run")
async def run_graph(request: Request) -> dict:
    pipeline = getattr(request.app.state, "nightly_graph_pipeline", None)
    if pipeline is None:
        raise HTTPException(status_code=500, detail="nightly graph pipeline not initialized")
    result = await pipeline.run()
    return {"ok": result.ok, "message": result.message}
