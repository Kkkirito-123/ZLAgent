"""Gateway runtime counters + status snapshot.

Sits between :class:`MessageGateway` and the public ``/api/gateways``
surface. Each gateway owns one :class:`GatewayCounters` instance the
manager mutates on inbound / outbound / failure; a :class:`GatewayStatus`
bundles those counters plus the gateway's static identity (name, kind)
and live ``configured`` / adapter-specific extras.

Defaults are tuned so an offline smoke (no real channel ever sends a
message) still returns a coherent JSON shape: zero counters, ``None``
timestamps, no ``last_error``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt is not None else None


@dataclass(slots=True)
class GatewayCounters:
    """Per-gateway runtime counters.

    ``first_seen_at`` is set by the very first ``record_inbound`` call
    and never mutates afterwards — it's the operator-visible "since
    when did this gateway start receiving traffic" timestamp.

    ``last_error`` is cleared on a successful outbound after a failure
    so the dashboard doesn't show stale red after the channel
    self-recovers.
    """

    inbound: int = 0
    outbound: int = 0
    failures: int = 0

    first_seen_at: Optional[datetime] = None
    last_inbound_at: Optional[datetime] = None
    last_outbound_at: Optional[datetime] = None
    last_error: Optional[str] = None
    last_error_at: Optional[datetime] = None

    # ----- mutators -----
    def record_inbound(self) -> None:
        now = _utcnow()
        self.inbound += 1
        self.last_inbound_at = now
        if self.first_seen_at is None:
            self.first_seen_at = now

    def record_outbound(self) -> None:
        self.outbound += 1
        self.last_outbound_at = _utcnow()
        # A successful outbound after a failure clears the red flag —
        # the operator UI shows green again until the next failure.
        self.last_error = None
        self.last_error_at = None

    def record_failure(self, reason: str) -> None:
        self.failures += 1
        self.last_error = (reason or "").strip() or "unknown error"
        self.last_error_at = _utcnow()

    # ----- serialisation -----
    def to_dict(self) -> dict[str, Any]:
        return {
            "inbound": self.inbound,
            "outbound": self.outbound,
            "failures": self.failures,
            "first_seen_at": _iso(self.first_seen_at),
            "last_inbound_at": _iso(self.last_inbound_at),
            "last_outbound_at": _iso(self.last_outbound_at),
            "last_error": self.last_error,
            "last_error_at": _iso(self.last_error_at),
        }


@dataclass(slots=True)
class GatewayStatus:
    """Identity + live state snapshot for a single gateway.

    Composed by :meth:`GatewayManager.gateway_statuses` and rendered
    by ``/api/gateways`` / ``/api/gateways/{name}``. Adapter-specific
    knobs (saved_accounts, base_url, polling, …) live under ``extra``
    so the schema stays stable as new gateways come online.
    """

    name: str
    kind: str
    configured: bool
    counters: GatewayCounters
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "configured": bool(self.configured),
            "counters": self.counters.to_dict(),
            "extra": dict(self.extra),
        }


__all__ = ["GatewayCounters", "GatewayStatus"]
