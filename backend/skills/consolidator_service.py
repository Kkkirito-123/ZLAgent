"""SkillKnowledgeConsolidatorService — periodic consolidator runner (v0.31).

Mirrors :mod:`backend.skills.curator_service`. Runs
:meth:`SkillKnowledgeConsolidator.consolidate` on a slow cadence inside
the FastAPI lifespan asyncio loop. Pure-file work, no LLM, so the cost
is dominated by I/O and stays cheap even with many skills.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

from loguru import logger

from .consolidator import SkillKnowledgeConsolidator


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SkillKnowledgeConsolidatorService:
    """Run :class:`SkillKnowledgeConsolidator` periodically."""

    def __init__(
        self,
        consolidator: SkillKnowledgeConsolidator,
        *,
        warmup_seconds: int = 600,
        interval_seconds: int = 24 * 3600,
        enabled: bool = True,
    ) -> None:
        if warmup_seconds < 0:
            raise ValueError("warmup_seconds must be >= 0")
        if interval_seconds < 60:
            raise ValueError("interval_seconds must be >= 60")
        self._consolidator = consolidator
        self._warmup = warmup_seconds
        self._interval = interval_seconds
        self._enabled = enabled
        self._task: Optional[asyncio.Task[None]] = None
        self._stop = asyncio.Event()
        self._last_run_at: Optional[str] = None
        self._last_summary: Optional[dict] = None

    @property
    def last_run_at(self) -> Optional[str]:
        return self._last_run_at

    @property
    def last_summary(self) -> Optional[dict]:
        return self._last_summary

    async def start(self) -> None:
        if not self._enabled:
            logger.info("[knowledge] service disabled; not starting")
            return
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="zlagent-knowledge")
        logger.info(
            "[knowledge] service started (warmup={}s, interval={}s)",
            self._warmup, self._interval,
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        self._task = None
        logger.info("[knowledge] service stopped")

    async def run_once(self) -> Optional[dict]:
        """Run a single consolidation pass and return its summary dict."""
        try:
            report = await asyncio.to_thread(self._consolidator.consolidate)
            self._last_run_at = _utcnow().isoformat(timespec="seconds")
            self._last_summary = {
                "started_at": report.started_at,
                "finished_at": report.finished_at,
                "skills_processed": report.skills_processed,
                "pages_written": report.pages_written,
                "pages_unchanged": report.pages_unchanged,
                "errors": list(report.errors),
            }
            logger.info(
                "[knowledge] consolidate: skills={} written={} unchanged={} errors={}",
                report.skills_processed,
                report.pages_written,
                report.pages_unchanged,
                len(report.errors),
            )
            return self._last_summary
        except Exception as exc:  # noqa: BLE001
            logger.exception("[knowledge] run_once failed: {}", exc)
            return None

    # =================================================================
    # Internals
    # =================================================================

    async def _run(self) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=self._warmup)
            return
        except asyncio.TimeoutError:
            pass

        while not self._stop.is_set():
            try:
                await self.run_once()
            except Exception as exc:  # noqa: BLE001 — never let aux task die
                logger.exception("[knowledge] tick failed: {}", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
                return
            except asyncio.TimeoutError:
                continue
