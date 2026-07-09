"""Normalized gateway message models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class DeliveryTarget:
    """Normalized outbound recipient."""

    platform: str
    target_type: str
    target_id: str
    display_name: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.platform.strip():
            raise ValueError("target.platform must be non-empty")
        if not self.target_type.strip():
            raise ValueError("target.target_type must be non-empty")
        if not self.target_id.strip():
            raise ValueError("target.target_id must be non-empty")
        object.__setattr__(self, "metadata", dict(self.metadata))


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    """Normalized inbound message."""

    id: str
    platform: str
    user_id: str
    text: str
    reply_to: DeliveryTarget
    created_at: datetime = field(default_factory=utc_now)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("message.id must be non-empty")
        if not self.platform.strip():
            raise ValueError("message.platform must be non-empty")
        if not self.user_id.strip():
            raise ValueError("message.user_id must be non-empty")
        if not self.text.strip():
            raise ValueError("message.text must be non-empty")
        object.__setattr__(self, "text", self.text.strip())
        object.__setattr__(self, "metadata", dict(self.metadata))


@dataclass(frozen=True, slots=True)
class OutgoingMessage:
    """Normalized outbound message."""

    target: DeliveryTarget
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("outgoing.text must be non-empty")
        object.__setattr__(self, "metadata", dict(self.metadata))


class GatewayAdapter(Protocol):
    """Boundary for concrete IM/API gateway adapters."""

    async def send(self, message: OutgoingMessage) -> None:
        """Send a normalized outbound message."""
