"""Trace primitives for harness observability."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol
from uuid import uuid4


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SpanStatus(str, Enum):
    RUNNING = "running"
    OK = "ok"
    ERROR = "error"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class TraceEvent:
    """One timestamped trace event."""

    name: str
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("trace event name must be non-empty")
        object.__setattr__(self, "metadata", redact_mapping(self.metadata))


@dataclass(frozen=True, slots=True)
class TraceSpan:
    """One trace span."""

    id: str
    trace_id: str
    name: str
    parent_id: str | None = None
    status: SpanStatus = SpanStatus.RUNNING
    metadata: dict[str, Any] = field(default_factory=dict)
    events: tuple[TraceEvent, ...] = field(default_factory=tuple)
    started_at: datetime = field(default_factory=utc_now)
    ended_at: datetime | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("span.id must be non-empty")
        if not self.trace_id.strip():
            raise ValueError("span.trace_id must be non-empty")
        if not self.name.strip():
            raise ValueError("span.name must be non-empty")
        object.__setattr__(self, "metadata", redact_mapping(self.metadata))
        object.__setattr__(self, "events", tuple(self.events))

    def duration_ms(self) -> int | None:
        if self.ended_at is None:
            return None
        return int((self.ended_at - self.started_at).total_seconds() * 1000)


class TraceRecorder(Protocol):
    """Trace recording boundary."""

    def start_span(
        self,
        name: str,
        *,
        trace_id: str | None = None,
        parent_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TraceSpan:
        """Create a running span."""

    def record_event(
        self,
        span_id: str,
        name: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> TraceEvent:
        """Append an event to a span."""

    def end_span(
        self,
        span_id: str,
        *,
        status: SpanStatus = SpanStatus.OK,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TraceSpan:
        """End a running span."""

    def get_span(self, span_id: str) -> TraceSpan | None:
        """Return a span by id."""

    def list_spans(self, *, trace_id: str | None = None) -> tuple[TraceSpan, ...]:
        """Return spans, optionally filtered by trace id."""


class InMemoryTraceRecorder:
    """In-memory trace recorder used as adapter behavior baseline."""

    def __init__(self) -> None:
        self._spans: dict[str, TraceSpan] = {}
        self._order: list[str] = []

    def start_span(
        self,
        name: str,
        *,
        trace_id: str | None = None,
        parent_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TraceSpan:
        if parent_id is not None and parent_id not in self._spans:
            raise ValueError(f"unknown parent span id: {parent_id}")
        span = TraceSpan(
            id=f"span_{uuid4().hex}",
            trace_id=trace_id or f"trace_{uuid4().hex}",
            name=name,
            parent_id=parent_id,
            metadata=metadata or {},
        )
        self._spans[span.id] = deepcopy(span)
        self._order.append(span.id)
        return deepcopy(span)

    def record_event(
        self,
        span_id: str,
        name: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> TraceEvent:
        span = self._require_span(span_id)
        if span.status is not SpanStatus.RUNNING:
            raise ValueError(f"cannot record event on ended span: {span_id}")
        event = TraceEvent(name=name, metadata=metadata or {})
        updated = TraceSpan(
            id=span.id,
            trace_id=span.trace_id,
            name=span.name,
            parent_id=span.parent_id,
            status=span.status,
            metadata=span.metadata,
            events=span.events + (event,),
            started_at=span.started_at,
            ended_at=span.ended_at,
            error=span.error,
        )
        self._spans[span_id] = updated
        return deepcopy(event)

    def end_span(
        self,
        span_id: str,
        *,
        status: SpanStatus = SpanStatus.OK,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TraceSpan:
        span = self._require_span(span_id)
        if span.status is not SpanStatus.RUNNING:
            raise ValueError(f"span already ended: {span_id}")
        merged_metadata = dict(span.metadata)
        merged_metadata.update(metadata or {})
        updated = TraceSpan(
            id=span.id,
            trace_id=span.trace_id,
            name=span.name,
            parent_id=span.parent_id,
            status=status,
            metadata=merged_metadata,
            events=span.events,
            started_at=span.started_at,
            ended_at=utc_now(),
            error=error,
        )
        self._spans[span_id] = updated
        return deepcopy(updated)

    def get_span(self, span_id: str) -> TraceSpan | None:
        span = self._spans.get(span_id)
        return deepcopy(span) if span is not None else None

    def list_spans(self, *, trace_id: str | None = None) -> tuple[TraceSpan, ...]:
        spans = [self._spans[span_id] for span_id in self._order]
        if trace_id is not None:
            spans = [span for span in spans if span.trace_id == trace_id]
        return deepcopy(tuple(spans))

    def _require_span(self, span_id: str) -> TraceSpan:
        span = self._spans.get(span_id)
        if span is None:
            raise ValueError(f"unknown span id: {span_id}")
        return span


_SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "password",
    "secret",
    "token",
)


def redact_mapping(value: dict[str, Any]) -> dict[str, Any]:
    """Redact common secret-shaped metadata fields."""

    redacted: dict[str, Any] = {}
    for key, item in dict(value).items():
        key_text = str(key)
        lowered = key_text.lower()
        if any(part in lowered for part in _SENSITIVE_KEY_PARTS):
            redacted[key_text] = "[REDACTED]"
        elif isinstance(item, dict):
            redacted[key_text] = redact_mapping(item)
        elif isinstance(item, list):
            redacted[key_text] = [
                redact_mapping(child) if isinstance(child, dict) else child
                for child in item
            ]
        else:
            redacted[key_text] = item
    return redacted
