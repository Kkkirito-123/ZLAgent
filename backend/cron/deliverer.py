from __future__ import annotations

from datetime import datetime
from typing import Optional

from loguru import logger

from ..db.models import CronJob, DeliveryTarget
from ..db.session import session_scope
from ..gateways.base import DeliveryTarget as RuntimeDeliveryTarget, OutgoingMessage
from .drafts import CronDraft, CronDraftStore


class CronDeliverer:
    def __init__(self, dispatch_fn, draft_store: CronDraftStore) -> None:
        self._dispatch = dispatch_fn
        self._drafts = draft_store

    async def send_preflight(self, job: CronJob, content: str) -> Optional[str]:
        target = self._resolve_target(job)
        if target is None:
            return None
        try:
            await self._dispatch(OutgoingMessage(target=target, text=content))
            self.mark_delivered(job.id)
            return content
        except Exception as exc:  # noqa: BLE001
            logger.warning("cron preflight dispatch failed for {}: {}", job.name, exc)
            self._drafts.mark_failed(job.id, f"preflight dispatch failed: {type(exc).__name__}: {exc}")
            return None

    def store_draft(self, draft: CronDraft) -> None:
        self._drafts.put(draft)

    def get_draft(self, job_id: int) -> Optional[CronDraft]:
        return self._drafts.get(job_id)

    def mark_delivered(self, job_id: int) -> None:
        self._drafts.mark_delivered(job_id)

    def pop_draft(self, job_id: int) -> Optional[CronDraft]:
        return self._drafts.pop(job_id)

    def _resolve_target(self, job: CronJob) -> Optional[RuntimeDeliveryTarget]:
        if job.delivery_target_id is None:
            return None
        with session_scope() as session:
            row = session.get(DeliveryTarget, job.delivery_target_id)
            if row is None:
                return None
            return RuntimeDeliveryTarget(
                platform=row.platform,
                target_type=row.target_type,
                target_id=row.target_id,
                display_name=row.display_name,
            )
