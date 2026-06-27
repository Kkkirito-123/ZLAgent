"""Background SkillCuratorService — periodic skill review (v0.11).

Wakes on a configurable cadence (default: 5-minute warmup, then every 12
hours) and runs :meth:`SkillCurator.run_review`. **The service itself
never mutates anything** — it only reports. Two reasons:

1. Conservative-by-default. Auto-archiving an "agent" skill that the
   user had grown fond of would be a terrible failure mode; making the
   user (or an LLM with explicit confirmation) commit the change keeps
   the trust gradient steep.

2. Visibility. The reports show up in the FastAPI log and via
   ``/api/curator/state`` so the operator can manually act on the
   suggestions (or wire up a policy in v0.12+ to act automatically on
   only the strongest signals).

The service is a single asyncio task spawned from the FastAPI
``lifespan``. Cancellation is graceful: the task respects an
:class:`asyncio.Event` so the lifespan teardown returns promptly.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

from loguru import logger

from .curator import SkillCurator


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SkillCuratorService:
    """Run :class:`SkillCurator` reviews periodically inside the asyncio loop."""

    def __init__(
        self,
        curator: SkillCurator,
        *,
        warmup_seconds: int = 300,
        interval_seconds: int = 12 * 3600,
        enabled: bool = True,
    ) -> None:
        if warmup_seconds < 0:
            raise ValueError("warmup_seconds must be >= 0")
        if interval_seconds < 60:
            # Below 60s the curator review starts dominating the agent's
            # CPU time — refuse anything that aggressive.
            raise ValueError("interval_seconds must be >= 60")
        self._curator = curator
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
            logger.info("[curator] service disabled; not starting")
            return
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="zlagent-curator")
        logger.info(
            "[curator] service started (warmup={}s, interval={}s)",
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
        logger.info("[curator] service stopped")

    async def run_once(self) -> Optional[dict]:
        """Synchronous-ish convenience for ``/api/curator/run``.

        Schedules the review on the running loop and returns the report
        dict. Failures are logged and ``None`` is returned so the HTTP
        handler can surface the error without polluting the call stack.
        """
        try:
            report = await asyncio.to_thread(self._curator.run_review)
            self._last_run_at = _utcnow().isoformat(timespec="seconds")
            self._last_summary = self._summary_dict(report)
            self._log_report(report)
            return self._last_summary
        except Exception as exc:  # noqa: BLE001
            logger.exception("[curator] run_once failed: {}", exc)
            return None

    # =================================================================
    # Internals
    # =================================================================

    async def _run(self) -> None:
        # Warmup window — let the rest of the FastAPI app finish booting
        # and any first-tick cron job complete before we add load.
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=self._warmup)
            return  # stopped during warmup
        except asyncio.TimeoutError:
            pass

        while not self._stop.is_set():
            try:
                await self.run_once()
            except Exception as exc:  # noqa: BLE001 - never let aux task die
                logger.exception("[curator] tick failed: {}", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
                return
            except asyncio.TimeoutError:
                continue

    @staticmethod
    def _summary_dict(report) -> dict:  # noqa: ANN001 - importing CuratorReport for sig is overkill
        return {
            "total_skills": report.total_skills,
            "agent_created_count": report.agent_created_count,
            "healthy": report.healthy,
            "stale_candidates": list(report.stale_candidates),
            "archive_candidates": list(report.archive_candidates),
            "pinned_skipped": list(report.pinned_skipped),
            "untracked": list(report.untracked),
            "duplicate_candidates": [
                {
                    "merge_from": c.merge_from,
                    "merge_into": c.merge_into,
                    "similarity": c.similarity,
                    "name_score": c.name_score,
                    "description_score": c.description_score,
                    "body_score": c.body_score,
                    "blockers": list(c.blockers),
                }
                for c in report.duplicate_candidates
            ],
        }

    @staticmethod
    def _log_report(report) -> None:  # noqa: ANN001
        logger.info(
            "[curator] report: total={} agent_created={} healthy={}"
            " stale={} archive={} pinned_skipped={} dup_pairs={}",
            report.total_skills,
            report.agent_created_count,
            report.healthy,
            len(report.stale_candidates),
            len(report.archive_candidates),
            len(report.pinned_skipped),
            len(report.duplicate_candidates),
        )
        if report.stale_candidates:
            logger.info("[curator] stale candidates: {}", report.stale_candidates)
        if report.archive_candidates:
            logger.info("[curator] archive candidates: {}", report.archive_candidates)
        for cand in report.duplicate_candidates:
            logger.info(
                "[curator] dup: {!r} -> {!r} sim={} (n={}, d={}, b={}){}",
                cand.merge_from, cand.merge_into,
                cand.similarity, cand.name_score,
                cand.description_score, cand.body_score,
                f" blockers={cand.blockers}" if cand.blockers else "",
            )
