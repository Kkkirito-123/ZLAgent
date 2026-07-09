"""Observability boundaries for traces and diagnostics."""

from .doctor import (
    DoctorCheck,
    DoctorReport,
    DoctorRunner,
    DoctorStatus,
    SupportBundle,
    SupportBundleBuilder,
)
from .health import build_harness_doctor, check_harness_facade
from .tracing import (
    InMemoryTraceRecorder,
    SpanStatus,
    TraceEvent,
    TraceRecorder,
    TraceSpan,
    redact_mapping,
)

__all__ = [
    "DoctorCheck",
    "DoctorReport",
    "DoctorRunner",
    "DoctorStatus",
    "InMemoryTraceRecorder",
    "SpanStatus",
    "SupportBundle",
    "SupportBundleBuilder",
    "TraceEvent",
    "TraceRecorder",
    "TraceSpan",
    "build_harness_doctor",
    "check_harness_facade",
    "redact_mapping",
]
