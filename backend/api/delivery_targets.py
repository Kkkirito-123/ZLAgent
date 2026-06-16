"""CRUD + test send for :class:`DeliveryTarget` records."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ..db.models import DeliveryTarget
from ..db.session import get_db
from ..gateways.base import DeliveryTarget as DeliveryTargetDTO
from ..gateways.base import OutgoingMessage

router = APIRouter(prefix="/api/delivery-targets", tags=["delivery-targets"])


class DeliveryTargetIn(BaseModel):
    platform: str = Field(
        description="Channel platform identifier, e.g. 'wecom_bot', 'webhook'.",
    )
    target_type: str = Field(default="group", description="user | group | channel | email")
    target_id: str = Field(description="Platform-specific target (webhook URL, chat id, email, ...)")
    display_name: str = Field(default="", description="Human-readable label for the UI.")
    enabled: bool = True


class DeliveryTargetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    platform: str
    target_type: str
    target_id: str
    display_name: str
    enabled: bool
    created_at: datetime


@router.get("", response_model=list[DeliveryTargetOut])
def list_targets(db: Session = Depends(get_db)) -> list[DeliveryTarget]:
    return list(db.query(DeliveryTarget).order_by(DeliveryTarget.id.asc()).all())


@router.post("", response_model=DeliveryTargetOut, status_code=201)
def create_target(payload: DeliveryTargetIn, db: Session = Depends(get_db)) -> DeliveryTarget:
    target = DeliveryTarget(
        platform=payload.platform,
        target_type=payload.target_type,
        target_id=payload.target_id,
        display_name=payload.display_name,
        enabled=payload.enabled,
    )
    db.add(target)
    db.commit()
    db.refresh(target)
    return target


@router.get("/{target_id}", response_model=DeliveryTargetOut)
def get_target(target_id: int, db: Session = Depends(get_db)) -> DeliveryTarget:
    target = db.get(DeliveryTarget, target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="delivery target not found")
    return target


@router.put("/{target_id}", response_model=DeliveryTargetOut)
def update_target(
    target_id: int,
    payload: DeliveryTargetIn,
    db: Session = Depends(get_db),
) -> DeliveryTarget:
    target = db.get(DeliveryTarget, target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="delivery target not found")
    target.platform = payload.platform
    target.target_type = payload.target_type
    target.target_id = payload.target_id
    target.display_name = payload.display_name
    target.enabled = payload.enabled
    db.commit()
    db.refresh(target)
    return target


@router.delete("/{target_id}", status_code=204)
def delete_target(target_id: int, db: Session = Depends(get_db)) -> None:
    target = db.get(DeliveryTarget, target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="delivery target not found")
    db.delete(target)
    db.commit()


class TestSendPayload(BaseModel):
    text: Optional[str] = Field(
        default=None,
        description="Override the default test message text.",
    )


@router.post("/{target_id}/test")
async def test_send(
    target_id: int,
    request: Request,
    payload: Optional[TestSendPayload] = None,
    db: Session = Depends(get_db),
) -> dict:
    target = db.get(DeliveryTarget, target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="delivery target not found")
    if not target.enabled:
        raise HTTPException(status_code=409, detail="delivery target is disabled")

    manager = request.app.state.gateway_manager
    if target.platform not in manager.platforms():
        raise HTTPException(
            status_code=409,
            detail=(
                f"no gateway registered for platform '{target.platform}';"
                f" registered platforms: {list(manager.platforms())}"
            ),
        )

    # ``payload`` is Optional so callers can POST with no body. Guard
    # against ``None`` before reading ``.text``; without this the
    # endpoint 500s on the common "fire a test ping" call.
    text = (payload.text if payload else None) or (
        f"[ZLAgent] test message for '{target.display_name or target.target_id}'"
    )
    outgoing = OutgoingMessage(
        target=DeliveryTargetDTO(
            platform=target.platform,
            target_type=target.target_type,
            target_id=target.target_id,
            display_name=target.display_name,
        ),
        text=text,
    )
    try:
        await manager.dispatch(outgoing)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"delivery failed: {exc}") from exc
    return {"status": "sent", "platform": target.platform, "text": text}
