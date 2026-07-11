"""Durable worker ownership and retry-state models."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


class RunLeaseState(str, Enum):
    """Current scheduling state for one run."""

    AVAILABLE = "available"
    ACTIVE = "active"
    BACKOFF = "backoff"
    RELEASED = "released"
    DEAD_LETTER = "dead_letter"


@dataclass(frozen=True, slots=True)
class RunLease:
    """Durable single-owner lease plus bounded retry projection."""

    run_id: str
    state: RunLeaseState = RunLeaseState.AVAILABLE
    owner_id: str | None = None
    lease_token: str | None = None
    acquired_at: datetime | None = None
    heartbeat_at: datetime | None = None
    expires_at: datetime | None = None
    attempt_count: int = 0
    retry_budget: int = 3
    next_attempt_at: datetime | None = None
    last_error: str | None = None
    release_reason: str | None = None
    version: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text(self.run_id, "run_lease.run_id")
        if self.attempt_count < 0:
            raise ValueError("run_lease.attempt_count must be >= 0")
        if self.retry_budget <= 0:
            raise ValueError("run_lease.retry_budget must be > 0")
        if self.version < 0:
            raise ValueError("run_lease.version must be >= 0")
        for name in ("acquired_at", "heartbeat_at", "expires_at", "next_attempt_at"):
            value = getattr(self, name)
            if value is not None:
                _require_aware(value, f"run_lease.{name}")
        object.__setattr__(self, "metadata", dict(self.metadata))
        if self.state is RunLeaseState.ACTIVE:
            if not self.owner_id or not self.lease_token or self.expires_at is None:
                raise ValueError("active run lease requires owner, token, and expiry")
        elif self.owner_id is not None or self.lease_token is not None:
            raise ValueError("inactive run lease cannot retain owner or token")
        if self.state is RunLeaseState.BACKOFF and self.next_attempt_at is None:
            raise ValueError("backoff run lease requires next_attempt_at")

    def active_at(self, now: datetime | None = None) -> bool:
        observed_at = now or _utc_now()
        _require_aware(observed_at, "now")
        return (
            self.state is RunLeaseState.ACTIVE
            and self.expires_at is not None
            and self.expires_at > observed_at
        )

    def claimable_at(self, now: datetime | None = None) -> bool:
        observed_at = now or _utc_now()
        _require_aware(observed_at, "now")
        if self.state is RunLeaseState.DEAD_LETTER:
            return False
        if self.state is RunLeaseState.ACTIVE:
            return not self.active_at(observed_at)
        if self.state is RunLeaseState.BACKOFF:
            return (
                self.next_attempt_at is not None
                and self.next_attempt_at <= observed_at
            )
        return True

    def claim(
        self,
        *,
        owner_id: str,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> "RunLease":
        _require_text(owner_id, "run_lease.owner_id")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be > 0")
        observed_at = now or _utc_now()
        _require_aware(observed_at, "now")
        if not self.claimable_at(observed_at):
            raise ValueError("run lease is not claimable")
        if self.attempt_count >= self.retry_budget:
            raise ValueError("run lease retry budget is exhausted")
        metadata = dict(self.metadata)
        if self.owner_id:
            metadata["previous_owner_id"] = self.owner_id
        if self.state is RunLeaseState.ACTIVE:
            metadata["reclaimed_expired_lease"] = True
        return replace(
            self,
            state=RunLeaseState.ACTIVE,
            owner_id=owner_id,
            lease_token=f"lease_{uuid4().hex}",
            acquired_at=observed_at,
            heartbeat_at=observed_at,
            expires_at=observed_at + timedelta(seconds=lease_seconds),
            attempt_count=self.attempt_count + 1,
            next_attempt_at=None,
            release_reason=None,
            version=self.version + 1,
            metadata=metadata,
        )

    def heartbeat(
        self,
        *,
        owner_id: str,
        lease_token: str,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> "RunLease":
        observed_at = now or _utc_now()
        _require_aware(observed_at, "now")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be > 0")
        self._require_owner(owner_id, lease_token)
        if not self.active_at(observed_at):
            raise ValueError("run lease has expired")
        return replace(
            self,
            heartbeat_at=observed_at,
            expires_at=observed_at + timedelta(seconds=lease_seconds),
            version=self.version + 1,
        )

    def release(
        self,
        *,
        owner_id: str,
        lease_token: str,
        state: RunLeaseState,
        reason: str,
        next_attempt_at: datetime | None = None,
        last_error: str | None = None,
    ) -> "RunLease":
        if state not in {
            RunLeaseState.AVAILABLE,
            RunLeaseState.BACKOFF,
            RunLeaseState.RELEASED,
            RunLeaseState.DEAD_LETTER,
        }:
            raise ValueError("release state must not be active")
        _require_text(reason, "run_lease.release_reason")
        self._require_owner(owner_id, lease_token)
        if state is RunLeaseState.BACKOFF and next_attempt_at is None:
            raise ValueError("backoff release requires next_attempt_at")
        if next_attempt_at is not None:
            _require_aware(next_attempt_at, "run_lease.next_attempt_at")
        metadata = dict(self.metadata)
        metadata["last_owner_id"] = owner_id
        return replace(
            self,
            state=state,
            owner_id=None,
            lease_token=None,
            heartbeat_at=None,
            expires_at=None,
            next_attempt_at=next_attempt_at,
            last_error=last_error,
            release_reason=reason,
            version=self.version + 1,
            metadata=metadata,
        )

    def dead_letter(self, reason: str) -> "RunLease":
        _require_text(reason, "run_lease.release_reason")
        metadata = dict(self.metadata)
        if self.owner_id:
            metadata["last_owner_id"] = self.owner_id
        return replace(
            self,
            state=RunLeaseState.DEAD_LETTER,
            owner_id=None,
            lease_token=None,
            heartbeat_at=None,
            expires_at=None,
            next_attempt_at=None,
            release_reason=reason,
            version=self.version + 1,
            metadata=metadata,
        )

    def _require_owner(self, owner_id: str, lease_token: str) -> None:
        if self.state is not RunLeaseState.ACTIVE:
            raise ValueError("run lease is not active")
        if self.owner_id != owner_id or self.lease_token != lease_token:
            raise ValueError("run lease owner or token does not match")

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "state": self.state.value,
            "owner_id": self.owner_id,
            "lease_token": self.lease_token,
            "acquired_at": self.acquired_at.isoformat() if self.acquired_at else None,
            "heartbeat_at": self.heartbeat_at.isoformat() if self.heartbeat_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "attempt_count": self.attempt_count,
            "retry_budget": self.retry_budget,
            "next_attempt_at": (
                self.next_attempt_at.isoformat() if self.next_attempt_at else None
            ),
            "last_error": self.last_error,
            "release_reason": self.release_reason,
            "version": self.version,
            "metadata": dict(self.metadata),
        }
