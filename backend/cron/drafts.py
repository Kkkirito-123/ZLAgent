from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional


@dataclass
class CronDraft:
    job_id: int
    job_name: str
    planned_fire_at: datetime
    generated_at: datetime
    content: str
    delivered: bool = False
    attempts: int = 0
    last_attempt_at: Optional[datetime] = None
    last_error: Optional[str] = None
    next_retry_at: Optional[datetime] = None
    meta: dict[str, object] = field(default_factory=dict)

    def schedule_retry(self, *, base_delay_seconds: int = 15) -> None:
        self.attempts += 1
        self.last_attempt_at = datetime.utcnow()
        delay = timedelta(seconds=base_delay_seconds * max(1, min(self.attempts, 6)))
        self.next_retry_at = self.last_attempt_at + delay


class CronDraftStore:
    def __init__(self) -> None:
        self._drafts: dict[int, CronDraft] = {}

    def put(self, draft: CronDraft) -> None:
        self._drafts[draft.job_id] = draft

    def get(self, job_id: int) -> Optional[CronDraft]:
        return self._drafts.get(job_id)

    def pending(self) -> list[CronDraft]:
        return [draft for draft in self._drafts.values() if not draft.delivered]

    def due_pending(self, now: Optional[datetime] = None) -> list[CronDraft]:
        now = now or datetime.utcnow()
        out: list[CronDraft] = []
        for draft in self.pending():
            if draft.next_retry_at is None or draft.next_retry_at <= now:
                out.append(draft)
        return out

    def mark_delivered(self, job_id: int) -> None:
        draft = self._drafts.get(job_id)
        if draft is not None:
            draft.delivered = True
            draft.last_error = None
            draft.next_retry_at = None

    def mark_failed(self, job_id: int, error: str) -> None:
        draft = self._drafts.get(job_id)
        if draft is not None:
            draft.last_error = error
            draft.schedule_retry()

    def pop(self, job_id: int) -> Optional[CronDraft]:
        return self._drafts.pop(job_id, None)
