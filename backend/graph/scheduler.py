from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from loguru import logger

from .builder import KnowledgeGraphBuilder
from .exporter import KnowledgeGraphExporter
from .source import GraphSourceBundle


@dataclass
class GraphJobResult:
    ok: bool
    message: str


class KnowledgeGraphScheduler:
    def __init__(
        self,
        *,
        source_fn: Callable[[str], list[dict[str, object]]],
        export_dir: Path,
        interval_seconds: int = 60,
    ) -> None:
        self._source_fn = source_fn
        self._export_dir = export_dir
        self._interval = max(10, int(interval_seconds))
        self._task: Optional[asyncio.Task[None]] = None
        self._stop_event = asyncio.Event()
        self._builder = KnowledgeGraphBuilder()
        self._exporter = KnowledgeGraphExporter()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="zlagent-graph")
        logger.info("knowledge graph scheduler started")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop_event.set()
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        self._task = None
        logger.info("knowledge graph scheduler stopped")

    async def run_once(self, knowledge_base_id: str = "default") -> GraphJobResult:
        bundle = self._source_fn(knowledge_base_id)
        snapshot = self._builder.build_from_records(bundle.records)
        self._export_dir.mkdir(parents=True, exist_ok=True)
        out = self._export_dir / f"graph-{knowledge_base_id}.json"
        self._exporter.export_json(snapshot, out)
        return GraphJobResult(
            ok=True,
            message=(
                f"exported {len(snapshot.nodes)} nodes, "
                f"{len(snapshot.edges)} edges for {knowledge_base_id}"
            ),
        )

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.run_once()
            except Exception as exc:  # noqa: BLE001
                logger.exception("knowledge graph tick failed: {}", exc)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                continue
