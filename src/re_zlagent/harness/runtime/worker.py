"""Durable single-owner background worker."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Callable

from re_zlagent.harness.storage import TaskStore
from re_zlagent.harness.tasking import (
    RunLease,
    RunLeaseState,
    TaskEventType,
    TaskRun,
    TaskRunStatus,
)

from .lifecycle import HarnessRuntime, RuntimeResult
from .outbox import InjectedOutboxCrash


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class WorkerTickStatus(str, Enum):
    """Outcome of one worker polling cycle."""

    NO_CANDIDATE = "no_candidate"
    COMPLETED = "completed"
    PARKED = "parked"
    RETRY_SCHEDULED = "retry_scheduled"
    DEAD_LETTER = "dead_letter"
    LEASE_LOST = "lease_lost"


@dataclass(frozen=True, slots=True)
class WorkerTickResult:
    """Structured result for one claimed or empty worker tick."""

    worker_id: str
    status: WorkerTickStatus
    run_id: str | None = None
    reason: str = ""
    lease: RunLease | None = None
    runtime_result: RuntimeResult | None = None


class DurableWorker:
    """Claim one run, execute through HarnessRuntime, and release durably."""

    _claimable_statuses = (
        TaskRunStatus.CREATED,
        TaskRunStatus.RUNNING,
        TaskRunStatus.RECOVERING,
    )

    def __init__(
        self,
        *,
        worker_id: str,
        store: TaskStore,
        runtime: HarnessRuntime,
        lease_seconds: int = 30,
        retry_budget: int = 3,
        base_backoff_seconds: int = 5,
        max_backoff_seconds: int = 300,
        scan_limit: int = 100,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not worker_id.strip():
            raise ValueError("worker_id must be non-empty")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be > 0")
        if retry_budget <= 0:
            raise ValueError("retry_budget must be > 0")
        if base_backoff_seconds <= 0 or max_backoff_seconds <= 0:
            raise ValueError("backoff seconds must be > 0")
        if scan_limit <= 0:
            raise ValueError("scan_limit must be > 0")
        self._worker_id = worker_id
        self._store = store
        self._runtime = runtime
        self._lease_seconds = lease_seconds
        self._retry_budget = retry_budget
        self._base_backoff_seconds = base_backoff_seconds
        self._max_backoff_seconds = max_backoff_seconds
        self._scan_limit = scan_limit
        self._clock = clock or _utc_now

    async def run_once(self, run_id: str | None = None) -> WorkerTickResult:
        """Claim and process at most one eligible run."""

        candidates = self._candidates(run_id)
        for run in candidates:
            lease = self._store.claim_run(
                run.id,
                owner_id=self._worker_id,
                lease_seconds=self._lease_seconds,
                retry_budget=self._retry_budget,
                now=self._clock(),
            )
            if lease is None:
                state = self._store.get_run_lease(run.id)
                if state is not None and state.state is RunLeaseState.DEAD_LETTER:
                    self._append_dead_letter_event(run.id, state)
                    return WorkerTickResult(
                        worker_id=self._worker_id,
                        run_id=run.id,
                        status=WorkerTickStatus.DEAD_LETTER,
                        reason=state.release_reason or "retry budget exhausted",
                        lease=state,
                    )
                continue
            if lease.expires_at is None:
                raise RuntimeError("claimed worker lease has no expiry")
            self._store.append_event(
                run_id=run.id,
                type=TaskEventType.RUN_CLAIMED,
                payload={
                    "worker_id": self._worker_id,
                    "lease_token": lease.lease_token,
                    "attempt_count": lease.attempt_count,
                    "retry_budget": lease.retry_budget,
                    "expires_at": lease.expires_at.isoformat(),
                },
                idempotency_key=f"worker-claim:{lease.lease_token}",
            )
            return await self._execute_claimed(run.id, lease)
        return WorkerTickResult(
            worker_id=self._worker_id,
            status=WorkerTickStatus.NO_CANDIDATE,
            reason="no claimable run",
        )

    def heartbeat(self, run_id: str, lease: RunLease) -> RunLease:
        """Extend one lease and append an auditable heartbeat event."""

        if lease.lease_token is None:
            raise ValueError("active lease token is required")
        updated = self._store.heartbeat_run_lease(
            run_id,
            owner_id=self._worker_id,
            lease_token=lease.lease_token,
            lease_seconds=self._lease_seconds,
            now=self._clock(),
        )
        if updated.expires_at is None:
            raise RuntimeError("heartbeat returned a lease without expiry")
        self._store.append_event(
            run_id=run_id,
            type=TaskEventType.RUN_HEARTBEAT,
            payload={
                "worker_id": self._worker_id,
                "lease_token": lease.lease_token,
                "expires_at": updated.expires_at.isoformat(),
                "lease_version": updated.version,
            },
            idempotency_key=(
                f"worker-heartbeat:{lease.lease_token}:{updated.version}"
            ),
        )
        return updated

    async def _execute_claimed(
        self,
        run_id: str,
        lease: RunLease,
    ) -> WorkerTickResult:
        stop_heartbeat = asyncio.Event()
        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(run_id, lease, stop_heartbeat)
        )
        try:
            runtime_result = await self._execute_runtime(run_id)
        except InjectedOutboxCrash:
            stop_heartbeat.set()
            await heartbeat_task
            raise
        except Exception as exc:  # noqa: BLE001 - worker failures become retry data
            stop_heartbeat.set()
            await heartbeat_task
            return self._release_exception(run_id, lease, exc)
        finally:
            stop_heartbeat.set()
        await heartbeat_task
        return self._release_result(run_id, lease, runtime_result)

    async def _execute_runtime(self, run_id: str) -> RuntimeResult:
        run = self._store.get_run(run_id)
        if run is None:
            raise ValueError(f"unknown run id: {run_id}")
        if run.status is TaskRunStatus.RECOVERING:
            return await self._runtime.resume_from_checkpoint(run_id=run_id)
        if run.status in {TaskRunStatus.CREATED, TaskRunStatus.RUNNING}:
            return await self._runtime.resume_incomplete_run(run_id=run_id)
        raise ValueError(f"run is not worker-executable: {run.status.value}")

    async def _heartbeat_loop(
        self,
        run_id: str,
        lease: RunLease,
        stop: asyncio.Event,
    ) -> None:
        interval = max(0.05, self._lease_seconds / 3)
        current = lease
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            except TimeoutError:
                try:
                    current = self.heartbeat(run_id, current)
                except ValueError:
                    return

    def _release_result(
        self,
        run_id: str,
        lease: RunLease,
        result: RuntimeResult,
    ) -> WorkerTickResult:
        run = result.run
        if run.status is TaskRunStatus.COMPLETED:
            return self._release(
                run_id,
                lease,
                state=RunLeaseState.RELEASED,
                tick_status=WorkerTickStatus.COMPLETED,
                reason="run completed",
                runtime_result=result,
            )
        if run.status in {
            TaskRunStatus.WAITING_USER,
            TaskRunStatus.PAUSED,
            TaskRunStatus.CANCELLED,
        }:
            return self._release(
                run_id,
                lease,
                state=RunLeaseState.RELEASED,
                tick_status=WorkerTickStatus.PARKED,
                reason=f"run parked: {run.status.value}",
                runtime_result=result,
            )
        if (
            run.status is TaskRunStatus.RECOVERING
            and result.failure is not None
            and result.failure.recoverable
        ):
            if lease.attempt_count >= lease.retry_budget:
                return self._release(
                    run_id,
                    lease,
                    state=RunLeaseState.DEAD_LETTER,
                    tick_status=WorkerTickStatus.DEAD_LETTER,
                    reason="retry budget exhausted",
                    runtime_result=result,
                    last_error=result.failure.root_cause,
                )
            delay = min(
                self._max_backoff_seconds,
                self._base_backoff_seconds * (2 ** (lease.attempt_count - 1)),
            )
            return self._release(
                run_id,
                lease,
                state=RunLeaseState.BACKOFF,
                tick_status=WorkerTickStatus.RETRY_SCHEDULED,
                reason="recoverable failure scheduled for retry",
                runtime_result=result,
                next_attempt_at=self._clock() + timedelta(seconds=delay),
                last_error=result.failure.root_cause,
            )
        failure_reason = (
            result.failure.root_cause
            if result.failure is not None
            else f"run stopped in status {run.status.value}"
        )
        return self._release(
            run_id,
            lease,
            state=RunLeaseState.DEAD_LETTER,
            tick_status=WorkerTickStatus.DEAD_LETTER,
            reason="run requires manual review",
            runtime_result=result,
            last_error=failure_reason,
        )

    def _release_exception(
        self,
        run_id: str,
        lease: RunLease,
        exc: Exception,
    ) -> WorkerTickResult:
        if lease.attempt_count >= lease.retry_budget:
            state = RunLeaseState.DEAD_LETTER
            tick_status = WorkerTickStatus.DEAD_LETTER
            next_attempt_at = None
        else:
            state = RunLeaseState.BACKOFF
            tick_status = WorkerTickStatus.RETRY_SCHEDULED
            delay = min(
                self._max_backoff_seconds,
                self._base_backoff_seconds * (2 ** (lease.attempt_count - 1)),
            )
            next_attempt_at = self._clock() + timedelta(seconds=delay)
        return self._release(
            run_id,
            lease,
            state=state,
            tick_status=tick_status,
            reason=f"worker execution raised {type(exc).__name__}",
            next_attempt_at=next_attempt_at,
            last_error=str(exc),
        )

    def _release(
        self,
        run_id: str,
        lease: RunLease,
        *,
        state: RunLeaseState,
        tick_status: WorkerTickStatus,
        reason: str,
        runtime_result: RuntimeResult | None = None,
        next_attempt_at: datetime | None = None,
        last_error: str | None = None,
    ) -> WorkerTickResult:
        if lease.lease_token is None:
            raise ValueError("active lease token is required")
        try:
            released = self._store.release_run_lease(
                run_id,
                owner_id=self._worker_id,
                lease_token=lease.lease_token,
                state=state,
                reason=reason,
                next_attempt_at=next_attempt_at,
                last_error=last_error,
            )
        except ValueError as exc:
            return WorkerTickResult(
                worker_id=self._worker_id,
                run_id=run_id,
                status=WorkerTickStatus.LEASE_LOST,
                reason=str(exc),
                runtime_result=runtime_result,
            )
        event_type = (
            TaskEventType.RUN_RETRY_SCHEDULED
            if state is RunLeaseState.BACKOFF
            else TaskEventType.RUN_DEAD_LETTERED
            if state is RunLeaseState.DEAD_LETTER
            else TaskEventType.RUN_LEASE_RELEASED
        )
        self._store.append_event(
            run_id=run_id,
            type=event_type,
            payload={
                "worker_id": self._worker_id,
                "state": state.value,
                "reason": reason,
                "attempt_count": released.attempt_count,
                "retry_budget": released.retry_budget,
                "next_attempt_at": (
                    next_attempt_at.isoformat() if next_attempt_at else None
                ),
                "last_error": last_error,
            },
            idempotency_key=(
                f"worker-release:{lease.lease_token}:{released.version}"
            ),
        )
        return WorkerTickResult(
            worker_id=self._worker_id,
            run_id=run_id,
            status=tick_status,
            reason=reason,
            lease=released,
            runtime_result=runtime_result,
        )

    def _append_dead_letter_event(self, run_id: str, lease: RunLease) -> None:
        self._store.append_event(
            run_id=run_id,
            type=TaskEventType.RUN_DEAD_LETTERED,
            payload={
                "worker_id": self._worker_id,
                "state": lease.state.value,
                "reason": lease.release_reason,
                "attempt_count": lease.attempt_count,
                "retry_budget": lease.retry_budget,
            },
            idempotency_key=f"worker-dead-letter:{run_id}:{lease.version}",
        )

    def _candidates(self, run_id: str | None) -> tuple[TaskRun, ...]:
        if run_id is not None:
            run = self._store.get_run(run_id)
            return (run,) if run is not None else ()
        return self._store.list_runs(
            statuses=self._claimable_statuses,
            limit=self._scan_limit,
        )
