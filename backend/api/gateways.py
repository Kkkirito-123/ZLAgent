"""REST surface for the gateway control plane.

Three endpoints:

* ``GET  /api/gateways``           — list every registered adapter
                                     with kind / configured / counters.
* ``GET  /api/gateways/{name}``    — single-adapter detail.
* ``POST /api/gateways/{name}/test`` — hand a synthetic
                                     :class:`IncomingMessage` to the
                                     adapter for plumbing checks.
                                     Bumps the inbound counter via the
                                     normal manager dispatch path so
                                     ops dashboards can verify the
                                     adapter is wired correctly without
                                     a real channel pinging it.

This router is mounted **after** ``backend.api.weixin`` so the legacy
``/api/gateways/weixin/{status,reload}`` routes still match first.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..gateways.base import DeliveryTarget, IncomingMessage


def _manager(request: Request):
    mgr = getattr(request.app.state, "gateway_manager", None)
    if mgr is None:
        raise HTTPException(status_code=500, detail="gateway manager not initialized")
    return mgr


class GatewayTestPayload(BaseModel):
    text: str = Field(default="", description="Message text the synthetic event carries.")
    user_id: str = Field(default="zlagent-test", description="User id stamped on the event.")
    channel_id: str = Field(default="zlagent-test", description="Channel id stamped on the event.")


def build_router() -> APIRouter:
    router = APIRouter(prefix="/api/gateways", tags=["gateways"])

    @router.get("")
    def list_gateways(request: Request) -> dict[str, Any]:
        mgr = _manager(request)
        statuses = mgr.gateway_statuses()
        return {
            "count": len(statuses),
            "items": [s.to_dict() for s in statuses],
        }

    @router.get("/{name}")
    def get_gateway(name: str, request: Request) -> dict[str, Any]:
        mgr = _manager(request)
        for s in mgr.gateway_statuses():
            if s.name == name:
                return s.to_dict()
        raise HTTPException(status_code=404, detail=f"unknown gateway '{name}'")

    @router.post("/{name}/test")
    async def test_gateway(
        name: str,
        request: Request,
        payload: Optional[GatewayTestPayload] = None,
    ) -> dict[str, Any]:
        mgr = _manager(request)
        gateway = mgr.get(name)
        if gateway is None:
            raise HTTPException(status_code=404, detail=f"unknown gateway '{name}'")

        body = payload or GatewayTestPayload()
        text = (body.text or "").strip() or "[ZLAgent gateway test ping]"
        ts = datetime.now(timezone.utc)
        message_id = f"test-{int(ts.timestamp())}"
        message = IncomingMessage(
            platform=name,
            channel_id=body.channel_id,
            user_id=body.user_id,
            message_id=message_id,
            text=text,
            timestamp=ts,
            reply_target=DeliveryTarget(
                platform=name,
                target_type="channel",
                target_id=body.channel_id,
            ),
            raw={"source": "/api/gateways/{name}/test"},
        )

        # Use the manager's normal entry point so counters update
        # the same way a real gateway emit would. Errors propagate
        # as 500 to keep the failure visible.
        try:
            await mgr.handle_incoming(message)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=500,
                detail=f"gateway test failed: {type(exc).__name__}: {exc}",
            ) from exc

        # Re-snapshot the gateway so the response reflects the bump.
        for s in mgr.gateway_statuses():
            if s.name == name:
                return {
                    "ok": True,
                    "name": name,
                    "message_id": message_id,
                    "counters": s.counters.to_dict(),
                }
        # Defensive: shouldn't happen, the gateway disappeared mid-call.
        return {"ok": True, "name": name, "message_id": message_id, "counters": {}}

    return router


__all__ = ["build_router"]
