"""Lightweight cron scheduler backed by ``croniter``.

The scheduler lives inside the FastAPI process as a single asyncio task. It
polls all enabled ``CronJob`` rows once per minute, fires due jobs, updates
their ``last_run_at`` / ``last_status`` fields, and hands execution off to a
user-supplied callable.

This is intentionally simple for v0.1: no distributed locks, no catch-up
replays, no per-job concurrency control. Those arrive once we have real jobs
that need them.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Awaitable, Callable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from loguru import logger
from sqlalchemy.orm import Session

from ..db.models import CronJob
from ..db.session import session_scope

JobRunner = Callable[[CronJob], Awaitable[str]]
TickHook = Callable[[], Awaitable[None]]
SHANGHAI_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")


def _job_zone(job: CronJob) -> tzinfo:
    timezone_name = getattr(job, "timezone", None) or "Asia/Shanghai"
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        logger.warning(
            "unknown timezone for job '{}': {!r}; falling back to UTC+08:00",
            job.name,
            timezone_name,
        )
        return SHANGHAI_TZ


def _as_zone(dt: datetime, zone: tzinfo) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc).astimezone(zone)
    return dt.astimezone(zone)


class CronScheduler:
    """Polls the cron_jobs table and triggers due jobs.

    Optionally runs a ``pre_tick_hook`` coroutine at the start of every
    tick. v0.6 uses this to age out pending confirmations on the same
    cadence as cron polling, without spinning up a separate asyncio task.
    """

    def __init__(
        self,
        runner: JobRunner,
        interval_seconds: int = 30,
        *,
        pre_tick_hook: Optional[TickHook] = None,
    ) -> None:
        self._runner = runner
        self._interval = interval_seconds
        self._pre_tick_hook = pre_tick_hook
        self._task: Optional[asyncio.Task[None]] = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="zlagent-cron")
        logger.info("cron scheduler started (interval={}s)", self._interval)

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
        logger.info("cron scheduler stopped")

    # Internal ----------------------------------------------------------------
    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._tick()
            except Exception as exc:  # pragma: no cover - defensive
                logger.exception("cron tick failed: {}", exc)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                continue

    async def _tick(self) -> None:
        if self._pre_tick_hook is not None:
            try:
                await self._pre_tick_hook()
            except Exception as exc:  # noqa: BLE001 - never let aux task kill cron
                logger.exception("pre-tick hook raised: {}", exc)
        now = datetime.utcnow()
        due_jobs: list[CronJob] = []
        with session_scope() as session:
            jobs = session.query(CronJob).filter(CronJob.enabled.is_(True)).all()
            for job in jobs:
                if self._is_due(job, now):
                    due_jobs.append(job)
            # detach so we can use jobs outside this session
            for job in due_jobs:
                session.expunge(job)

        for job in due_jobs:
            try:
                await self.execute(job)
            except Exception as exc:  # noqa: BLE001
                logger.exception("cron job '{}' failed: {}", job.name, exc)

    @staticmethod
    def _is_due(job: CronJob, now: datetime) -> bool:
        # Fire exactly when cron time arrives. ``lead_minutes`` is used to
        # trigger the work early so the final delivery lands at the scheduled
        # minute boundary.
        zone = _job_zone(job)
        now_local = _as_zone(now, zone)
        anchor = _as_zone(job.last_run_at or job.created_at or now, zone)
        try:
            itr = croniter(job.cron_expr, anchor)
        except (ValueError, KeyError):
            logger.warning("invalid cron expression for job '{}': {!r}", job.name, job.cron_expr)
            return False
        next_fire: datetime = itr.get_next(datetime)
        lead_minutes = max(0, int(getattr(job, "lead_minutes", 2) or 0))
        fire_at = next_fire - timedelta(minutes=lead_minutes)
        return fire_at <= now_local

    async def execute(self, job: CronJob) -> str:
        """Run ``job``'s runner, persist status, and re-raise on failure.

        Used by both the background tick and the manual ``/run`` endpoint so
        ``last_run_at`` / ``last_status`` / ``last_error`` always stay in sync
        regardless of who triggered the execution.
        """
        logger.info("running cron job '{}' ({}) lead_minutes={}", job.name, job.cron_expr, getattr(job, "lead_minutes", 2))
        status = "ok"
        result: str = "ok"
        error: Optional[str] = None
        try:
            returned = await self._runner(job)
            if isinstance(returned, str) and returned:
                result = returned
        except Exception as exc:  # noqa: BLE001
            status = "error"
            error = str(exc)
        self._record_run(job, status=status, error=error)
        if error is not None:
            raise RuntimeError(error)
        return result

    @staticmethod
    def _record_run(job: CronJob, *, status: str, error: Optional[str]) -> None:
        with session_scope() as session:
            persisted = session.get(CronJob, job.id)
            if persisted is None:
                return
            persisted.last_run_at = datetime.utcnow()
            persisted.last_status = status
            persisted.last_error = error
            if status == "ok" and bool(getattr(persisted, "run_once", False)):
                persisted.enabled = False
