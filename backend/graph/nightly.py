from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from loguru import logger

from ..db.models import DeliveryTarget
from ..db.session import session_scope
from ..gateways.base import DeliveryTarget as RuntimeDeliveryTarget
from .notifier import GraphNotification, GraphNotifier
from .scheduler import KnowledgeGraphScheduler


@dataclass
class NightlyGraphReport:
    ok: bool
    message: str


class NightlyGraphPipeline:
    def __init__(
        self,
        *,
        scheduler: KnowledgeGraphScheduler,
        export_dir: Path,
        dispatch_fn,
        notify_target_id: Optional[int] = None,
    ) -> None:
        self._scheduler = scheduler
        self._export_dir = export_dir
        self._notifier = GraphNotifier(dispatch_fn)
        self._notify_target_id = notify_target_id

    async def run(self, knowledge_base_id: str = "default") -> NightlyGraphReport:
        result = await self._scheduler.run_once(knowledge_base_id=knowledge_base_id)
        target = self._resolve_target()
        if target is not None:
            await self._notifier.send(
                GraphNotification(
                    title=f"[ZLAgent] nightly graph updated ({knowledge_base_id})",
                    summary=result.message,
                    target=target,
                )
            )
        return NightlyGraphReport(ok=result.ok, message=result.message)

    def _resolve_target(self) -> Optional[RuntimeDeliveryTarget]:
        if self._notify_target_id is None:
            return None
        with session_scope() as session:
            row = session.get(DeliveryTarget, self._notify_target_id)
            if row is None:
                return None
            if not row.enabled:
                return None
            return RuntimeDeliveryTarget(
                platform=row.platform,
                target_type=row.target_type,
                target_id=row.target_id,
                display_name=row.display_name,
            )
