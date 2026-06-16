"""Persistence layer for ``PendingConfirmation`` rows.

Wraps the raw SQLAlchemy model with intent-revealing operations the agent
loop and the resume path need. All public methods open their own short-lived
session via :func:`backend.db.session.session_scope` and return either
plain Python tuples / dicts or freshly-detached ORM rows; callers never have
to think about session lifetimes.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

from loguru import logger
from sqlalchemy import and_

from .models import PendingConfirmation
from .session import session_scope


# Status constants — kept here so callers don't sprinkle string literals.
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_DENIED = "denied"
STATUS_EXPIRED = "expired"
STATUS_RESOLVED = "resolved"  # set after the resumed turn completes successfully


@dataclass(slots=True)
class ConfirmationSnapshot:
    """Detached, plain-data view of a ``PendingConfirmation`` row.

    Returned by :class:`ConfirmationStore` so callers can use the data
    outside the originating session without worrying about ORM lifecycles.
    """

    id: int
    platform: str
    user_id: str
    reply_target: dict[str, Any]
    tool_name: str
    tool_arguments: dict[str, Any]
    tool_call_id: str
    history: list[dict[str, Any]]
    system_prompt: str
    question_text: str
    status: str
    created_at: datetime
    expires_at: datetime
    resolved_at: Optional[datetime]
    resolved_outcome: Optional[str]


def _to_snapshot(row: PendingConfirmation) -> ConfirmationSnapshot:
    return ConfirmationSnapshot(
        id=row.id,
        platform=row.platform,
        user_id=row.user_id,
        reply_target=json.loads(row.reply_target_json) if row.reply_target_json else {},
        tool_name=row.tool_name,
        tool_arguments=(
            json.loads(row.tool_arguments_json) if row.tool_arguments_json else {}
        ),
        tool_call_id=row.tool_call_id,
        history=json.loads(row.llm_history_json) if row.llm_history_json else [],
        system_prompt=row.system_prompt or "",
        question_text=row.question_text or "",
        status=row.status,
        created_at=row.created_at,
        expires_at=row.expires_at,
        resolved_at=row.resolved_at,
        resolved_outcome=row.resolved_outcome,
    )


class ConfirmationStore:
    """Repository over ``pending_confirmations``.

    The store is stateless; instances are cheap to create and safe to share
    across coroutines because every method opens its own session.
    """

    def __init__(self, *, default_ttl_seconds: int = 300) -> None:
        self._default_ttl = max(30, int(default_ttl_seconds))

    # -- create --------------------------------------------------------

    def create(
        self,
        *,
        platform: str,
        user_id: str,
        reply_target: dict[str, Any],
        tool_name: str,
        tool_arguments: dict[str, Any],
        tool_call_id: str,
        history: list[dict[str, Any]],
        system_prompt: str,
        question_text: str,
        ttl_seconds: Optional[int] = None,
    ) -> ConfirmationSnapshot:
        """Persist a new pending confirmation and return its snapshot.

        Caller is responsible for ensuring there is no overlapping pending
        for the same (platform, user_id) — see :meth:`find_pending_for`.
        """
        ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl
        now = datetime.utcnow()
        expires_at = now + timedelta(seconds=ttl)
        with session_scope() as session:
            row = PendingConfirmation(
                platform=platform,
                user_id=user_id,
                reply_target_json=json.dumps(reply_target, ensure_ascii=False),
                tool_name=tool_name,
                tool_arguments_json=json.dumps(tool_arguments, ensure_ascii=False),
                tool_call_id=tool_call_id,
                llm_history_json=json.dumps(history, ensure_ascii=False),
                system_prompt=system_prompt,
                question_text=question_text,
                status=STATUS_PENDING,
                created_at=now,
                expires_at=expires_at,
            )
            session.add(row)
            session.flush()
            session.refresh(row)
            snapshot = _to_snapshot(row)
        logger.info(
            "confirmation #{} created: platform={} user={} tool={} expires={}",
            snapshot.id, platform, user_id, tool_name, expires_at.isoformat(timespec="seconds"),
        )
        return snapshot

    # -- read ----------------------------------------------------------

    def find_pending_for(
        self, *, platform: str, user_id: str
    ) -> Optional[ConfirmationSnapshot]:
        """Return the **most recent** still-open confirmation for the user.

        Recency wins over fairness: when a user has somehow ended up with
        multiple open pendings (e.g. bot restart left orphans), the
        natural reading of a follow-up ``yes`` is "approve the question
        you just asked me", not the oldest stale one.
        """
        now = datetime.utcnow()
        with session_scope() as session:
            row = (
                session.query(PendingConfirmation)
                .filter(
                    and_(
                        PendingConfirmation.platform == platform,
                        PendingConfirmation.user_id == user_id,
                        PendingConfirmation.status == STATUS_PENDING,
                        PendingConfirmation.expires_at > now,
                    )
                )
                .order_by(PendingConfirmation.created_at.desc())
                .first()
            )
            if row is None:
                return None
            return _to_snapshot(row)

    def supersede_pending_for(
        self, *, platform: str, user_id: str, reason: str = "superseded by newer pending"
    ) -> int:
        """Mark every still-open pending for the user as ``expired``.

        Called by :class:`backend.agent.loop.AgentLoop` right before
        persisting a new pending so that the "one open question per user"
        invariant holds. Prevents the orphan-accumulation bug where
        several confirmations queue up over multiple turns.
        """
        now = datetime.utcnow()
        with session_scope() as session:
            rows = (
                session.query(PendingConfirmation)
                .filter(
                    and_(
                        PendingConfirmation.platform == platform,
                        PendingConfirmation.user_id == user_id,
                        PendingConfirmation.status == STATUS_PENDING,
                    )
                )
                .all()
            )
            count = 0
            for row in rows:
                row.status = STATUS_EXPIRED
                row.resolved_at = now
                row.resolved_outcome = reason
                count += 1
        if count:
            logger.info(
                "superseded {} prior pending confirmation(s) for {}/{}",
                count, platform, user_id,
            )
        return count

    def get(self, confirmation_id: int) -> Optional[ConfirmationSnapshot]:
        with session_scope() as session:
            row = session.get(PendingConfirmation, confirmation_id)
            return _to_snapshot(row) if row is not None else None

    def list_active(self, limit: int = 50) -> list[ConfirmationSnapshot]:
        """All currently-pending confirmations across users (ops view)."""
        now = datetime.utcnow()
        with session_scope() as session:
            rows = (
                session.query(PendingConfirmation)
                .filter(
                    and_(
                        PendingConfirmation.status == STATUS_PENDING,
                        PendingConfirmation.expires_at > now,
                    )
                )
                .order_by(PendingConfirmation.created_at.asc())
                .limit(limit)
                .all()
            )
            return [_to_snapshot(r) for r in rows]

    # -- transitions ---------------------------------------------------

    def mark(
        self,
        confirmation_id: int,
        *,
        status: str,
        outcome: Optional[str] = None,
    ) -> Optional[ConfirmationSnapshot]:
        """Move a confirmation to a terminal state (idempotent on already-resolved rows).

        Returns ``None`` if the row no longer exists. Returns the updated
        snapshot otherwise. We do NOT clobber an already-resolved row's
        status — once resolved, the original outcome wins and we just
        record a no-op-ish notice in ``resolved_outcome``.
        """
        if status not in {STATUS_APPROVED, STATUS_DENIED, STATUS_EXPIRED, STATUS_RESOLVED}:
            raise ValueError(f"invalid status: {status}")
        with session_scope() as session:
            row = session.get(PendingConfirmation, confirmation_id)
            if row is None:
                return None
            if row.status != STATUS_PENDING and status != STATUS_RESOLVED:
                # Don't override a previous decision; just emit a snapshot.
                logger.info(
                    "confirmation #{} already in status={!r}; ignoring transition to {!r}",
                    confirmation_id, row.status, status,
                )
                return _to_snapshot(row)
            row.status = status
            row.resolved_at = datetime.utcnow()
            if outcome is not None:
                row.resolved_outcome = outcome
            session.flush()
            session.refresh(row)
            return _to_snapshot(row)

    def expire_old(self) -> int:
        """Move all overdue pending rows to ``expired``. Returns the count.

        Called periodically by the cron scheduler tick.
        """
        now = datetime.utcnow()
        with session_scope() as session:
            rows = (
                session.query(PendingConfirmation)
                .filter(
                    and_(
                        PendingConfirmation.status == STATUS_PENDING,
                        PendingConfirmation.expires_at <= now,
                    )
                )
                .all()
            )
            count = 0
            for row in rows:
                row.status = STATUS_EXPIRED
                row.resolved_at = now
                row.resolved_outcome = "expired by sweeper"
                count += 1
        if count:
            logger.info("confirmation sweeper expired {} pending row(s)", count)
        return count
