"""HTTP control surface for the Weixin (personal WeChat) gateway."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api/gateways/weixin", tags=["gateway:weixin"])


def _get_gateway(request: Request):
    manager = getattr(request.app.state, "gateway_manager", None)
    if manager is None:
        raise HTTPException(status_code=500, detail="gateway manager not initialized")
    gateway = manager._gateways.get("weixin")  # noqa: SLF001 - intentional internal access
    if gateway is None:
        raise HTTPException(
            status_code=503, detail="weixin gateway not registered (missing dependencies?)"
        )
    return gateway


@router.get("/status")
async def status(request: Request) -> dict:
    return _get_gateway(request).status()


@router.post("/reload")
async def reload(request: Request) -> dict:
    gateway = _get_gateway(request)
    return await gateway.reload()
