"""HTTP surface for the v0.6 confirmation broker.

Endpoints:

* ``GET    /api/confirmations``                 — list currently-pending rows.
* ``GET    /api/confirmations/{id}``            — full snapshot incl. truncated history.
* ``POST   /api/confirmations/{id}/approve``    — operator-side yes (bypass IM).
* ``POST   /api/confirmations/{id}/deny``       — operator-side no (bypass IM).
* ``POST   /api/confirmations/expire-now``      — force-run the expiry sweeper.

The approve/deny endpoints exist so an operator can resolve a confirmation
when the user is unreachable (or when testing). They mark the row but do
NOT execute the tool — the IM resume path is the only one that actually
runs the tool, because doing so requires the AgentLoop to drive the
follow-up LLM step. v0.7 may add a "resume now" endpoint that wires both.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..db.confirmations import (
    STATUS_APPROVED,
    STATUS_DENIED,
    ConfirmationSnapshot,
    ConfirmationStore,
)

router = APIRouter(prefix="/api/confirmations", tags=["confirmations"])


def _get_store(request: Request) -> ConfirmationStore:
    store = getattr(request.app.state, "confirmation_store", None)
    if store is None:
        raise HTTPException(status_code=500, detail="confirmation store not initialised")
    return store


class ConfirmationRow(BaseModel):
    id: int
    platform: str
    user_id: str
    tool_name: str
    tool_arguments: dict[str, Any]
    status: str
    created_at: datetime
    expires_at: datetime
    resolved_at: Optional[datetime] = None
    resolved_outcome: Optional[str] = None
    history_message_count: int
    question_text: str = Field(default="", description="The IM question that was sent")


def _to_row(snap: ConfirmationSnapshot) -> ConfirmationRow:
    return ConfirmationRow(
        id=snap.id,
        platform=snap.platform,
        user_id=snap.user_id,
        tool_name=snap.tool_name,
        tool_arguments=snap.tool_arguments,
        status=snap.status,
        created_at=snap.created_at,
        expires_at=snap.expires_at,
        resolved_at=snap.resolved_at,
        resolved_outcome=snap.resolved_outcome,
        history_message_count=len(snap.history),
        question_text=snap.question_text,
    )


class ConfirmationListResponse(BaseModel):
    count: int
    items: list[ConfirmationRow]


@router.get("", response_model=ConfirmationListResponse)
async def list_confirmations(request: Request) -> ConfirmationListResponse:
    store = _get_store(request)
    rows = [_to_row(s) for s in store.list_active()]
    return ConfirmationListResponse(count=len(rows), items=rows)


@router.get("/{confirmation_id}", response_model=ConfirmationRow)
async def get_confirmation(confirmation_id: int, request: Request) -> ConfirmationRow:
    store = _get_store(request)
    snap = store.get(confirmation_id)
    if snap is None:
        raise HTTPException(status_code=404, detail=f"confirmation #{confirmation_id} not found")
    return _to_row(snap)


@router.post("/{confirmation_id}/approve", response_model=ConfirmationRow)
async def approve(confirmation_id: int, request: Request) -> ConfirmationRow:
    store = _get_store(request)
    updated = store.mark(
        confirmation_id, status=STATUS_APPROVED, outcome="operator-approved"
    )
    if updated is None:
        raise HTTPException(status_code=404, detail=f"confirmation #{confirmation_id} not found")
    return _to_row(updated)


@router.post("/{confirmation_id}/deny", response_model=ConfirmationRow)
async def deny(confirmation_id: int, request: Request) -> ConfirmationRow:
    store = _get_store(request)
    updated = store.mark(
        confirmation_id, status=STATUS_DENIED, outcome="operator-denied"
    )
    if updated is None:
        raise HTTPException(status_code=404, detail=f"confirmation #{confirmation_id} not found")
    return _to_row(updated)


class SweepResponse(BaseModel):
    expired: int


@router.post("/expire-now", response_model=SweepResponse)
async def expire_now(request: Request) -> SweepResponse:
    store = _get_store(request)
    return SweepResponse(expired=store.expire_old())
