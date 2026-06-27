"""A generic HTTP webhook gateway used for testing and as a reference adapter.

This gateway does **not** talk to any specific IM platform. It exposes two
things:

1. :func:`build_router` returns a FastAPI ``APIRouter`` that accepts POST
   /api/gateways/webhook requests and turns them into :class:`IncomingMessage`
   events.
2. Outgoing messages are written to the server log so end-to-end flows can be
   exercised without configuring a real channel.

Real WeCom/Feishu/email adapters will plug into the same
:class:`MessageGateway` interface without changing the agent core.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from loguru import logger
from pydantic import BaseModel, Field

from .base import (
    DeliveryTarget,
    IncomingMessage,
    MessageGateway,
    MessageHandler,
    OutgoingMessage,
)


class WebhookIncomingPayload(BaseModel):
    """Payload accepted by the webhook endpoint."""

    platform: str = Field(default="webhook")
    channel_id: str = Field(default="webhook")
    user_id: str = Field(default="anonymous")
    message_id: str = Field(default_factory=lambda: f"wh-{datetime.utcnow().timestamp():.0f}")
    text: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)


class WebhookGateway(MessageGateway):
    """Minimal gateway: logs outgoing, accepts incoming via HTTP."""

    platform = "webhook"
    kind = "http"

    def __init__(self, handler: MessageHandler) -> None:
        super().__init__(handler)

    async def start(self) -> None:
        logger.info("webhook gateway ready (POST /api/gateways/webhook)")

    async def stop(self) -> None:
        logger.info("webhook gateway stopped")

    async def send(self, message: OutgoingMessage) -> None:
        logger.info(
            "[webhook send] target={} text={!r}",
            message.target.describe(),
            message.text,
        )


def build_router(gateway: WebhookGateway) -> APIRouter:
    """Return the FastAPI router that pushes incoming events into ``gateway``."""

    router = APIRouter(prefix="/api/gateways/webhook", tags=["gateway:webhook"])

    @router.post("")
    async def receive(payload: WebhookIncomingPayload) -> dict[str, Any]:
        if not payload.text and not payload.meta:
            raise HTTPException(status_code=400, detail="empty payload")
        message = IncomingMessage(
            platform=payload.platform,
            channel_id=payload.channel_id,
            user_id=payload.user_id,
            message_id=payload.message_id,
            text=payload.text,
            timestamp=datetime.utcnow(),
            reply_target=DeliveryTarget(
                platform=payload.platform,
                target_type="channel",
                target_id=payload.channel_id,
            ),
            raw=payload.meta,
        )
        await gateway.emit(message)
        return {"status": "accepted", "message_id": message.message_id}

    return router
