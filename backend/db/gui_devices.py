"""Persistence helpers for OpenGUI device bindings."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy import and_

from .models import GuiDeviceBinding
from .session import session_scope


@dataclass(slots=True)
class GuiDeviceBindingSnapshot:
    id: int
    platform: str
    user_id: str
    opengui_base_url: str
    device_id: str
    device_name: str
    enabled: bool
    last_verified_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime


def _to_snapshot(row: GuiDeviceBinding) -> GuiDeviceBindingSnapshot:
    return GuiDeviceBindingSnapshot(
        id=row.id,
        platform=row.platform,
        user_id=row.user_id,
        opengui_base_url=row.opengui_base_url,
        device_id=row.device_id,
        device_name=row.device_name or "",
        enabled=bool(row.enabled),
        last_verified_at=row.last_verified_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class GuiDeviceBindingStore:
    """Small repository over ``gui_device_bindings``."""

    def get_current(
        self,
        *,
        platform: str,
        user_id: str,
        only_enabled: bool = True,
    ) -> Optional[GuiDeviceBindingSnapshot]:
        with session_scope() as session:
            query = session.query(GuiDeviceBinding).filter(
                and_(
                    GuiDeviceBinding.platform == platform,
                    GuiDeviceBinding.user_id == user_id,
                )
            )
            if only_enabled:
                query = query.filter(GuiDeviceBinding.enabled.is_(True))
            row = query.order_by(GuiDeviceBinding.updated_at.desc()).first()
            return _to_snapshot(row) if row is not None else None

    def bind(
        self,
        *,
        platform: str,
        user_id: str,
        opengui_base_url: str,
        device_id: str,
        device_name: str = "",
        verified_at: Optional[datetime] = None,
    ) -> GuiDeviceBindingSnapshot:
        now = datetime.utcnow()
        verified_at = verified_at or now
        with session_scope() as session:
            rows = (
                session.query(GuiDeviceBinding)
                .filter(
                    and_(
                        GuiDeviceBinding.platform == platform,
                        GuiDeviceBinding.user_id == user_id,
                    )
                )
                .all()
            )
            current: Optional[GuiDeviceBinding] = None
            for row in rows:
                if row.device_id == device_id and row.opengui_base_url == opengui_base_url:
                    current = row
                    break
                row.enabled = False
            if current is None:
                current = GuiDeviceBinding(
                    platform=platform,
                    user_id=user_id,
                    opengui_base_url=opengui_base_url,
                    device_id=device_id,
                )
                session.add(current)
            current.device_name = device_name or device_id
            current.enabled = True
            current.last_verified_at = verified_at
            current.updated_at = now
            session.flush()
            session.refresh(current)
            return _to_snapshot(current)

    def mark_verified(
        self,
        *,
        binding_id: int,
        verified_at: Optional[datetime] = None,
    ) -> Optional[GuiDeviceBindingSnapshot]:
        with session_scope() as session:
            row = session.get(GuiDeviceBinding, binding_id)
            if row is None:
                return None
            now = datetime.utcnow()
            row.last_verified_at = verified_at or now
            row.updated_at = now
            session.flush()
            session.refresh(row)
            return _to_snapshot(row)
