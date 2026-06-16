"""Core runtime overview endpoint."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from ..core.runtime import build_core_runtime_overview

router = APIRouter(prefix="/api/runtime", tags=["runtime"])


@router.get("")
async def runtime_overview(request: Request) -> dict[str, Any]:
    overview = build_core_runtime_overview(app=request.app)
    return overview.to_dict()


__all__ = ["router"]
