"""Doctor checks and support bundle primitives."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from .tracing import TraceSpan, redact_mapping


class DoctorStatus(str, Enum):
    OK = "ok"
    WARN = "warn"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    """One diagnostic check result."""

    name: str
    status: DoctorStatus
    message: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("doctor check name must be non-empty")
        object.__setattr__(self, "metadata", redact_mapping(self.metadata))


@dataclass(frozen=True, slots=True)
class DoctorReport:
    """Aggregated diagnostic report."""

    checks: tuple[DoctorCheck, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "checks", tuple(self.checks))

    @property
    def ok(self) -> bool:
        return not any(item.status is DoctorStatus.ERROR for item in self.checks)

    @property
    def worst_status(self) -> DoctorStatus:
        if any(item.status is DoctorStatus.ERROR for item in self.checks):
            return DoctorStatus.ERROR
        if any(item.status is DoctorStatus.WARN for item in self.checks):
            return DoctorStatus.WARN
        return DoctorStatus.OK


DoctorCheckFn = Callable[[], DoctorCheck]


class DoctorRunner:
    """Run named diagnostic checks."""

    def __init__(self) -> None:
        self._checks: dict[str, DoctorCheckFn] = {}

    def register(self, name: str, check: DoctorCheckFn) -> None:
        if not name.strip():
            raise ValueError("doctor check name must be non-empty")
        if name in self._checks:
            raise ValueError(f"duplicate doctor check: {name}")
        self._checks[name] = check

    def run(self) -> DoctorReport:
        results: list[DoctorCheck] = []
        for name, check in self._checks.items():
            try:
                result = check()
            except Exception as exc:  # noqa: BLE001 - diagnostics are data
                result = DoctorCheck(
                    name=name,
                    status=DoctorStatus.ERROR,
                    message=f"{type(exc).__name__}: {exc}",
                )
            if result.name != name:
                result = DoctorCheck(
                    name=name,
                    status=result.status,
                    message=result.message,
                    metadata=result.metadata,
                )
            results.append(result)
        return DoctorReport(checks=tuple(results))


@dataclass(frozen=True, slots=True)
class SupportBundle:
    """Redacted diagnostics package for issue reports."""

    title: str
    issue_summary: str
    diagnostics_manifest: dict[str, Any]


class SupportBundleBuilder:
    """Build redacted support bundles from explicit diagnostics."""

    def build(
        self,
        *,
        title: str,
        report: DoctorReport,
        traces: tuple[TraceSpan, ...] | list[TraceSpan] = (),
        metadata: dict[str, Any] | None = None,
    ) -> SupportBundle:
        if not title.strip():
            raise ValueError("support bundle title must be non-empty")
        manifest = {
            "doctor": {
                "ok": report.ok,
                "worst_status": report.worst_status.value,
                "checks": [
                    {
                        "name": check.name,
                        "status": check.status.value,
                        "message": check.message,
                        "metadata": check.metadata,
                    }
                    for check in report.checks
                ],
            },
            "traces": [
                {
                    "id": span.id,
                    "trace_id": span.trace_id,
                    "name": span.name,
                    "status": span.status.value,
                    "metadata": span.metadata,
                    "event_count": len(span.events),
                    "duration_ms": span.duration_ms(),
                }
                for span in traces
            ],
            "metadata": redact_mapping(metadata or {}),
        }
        issue_summary = self._summary(title=title, report=report)
        return SupportBundle(
            title=title,
            issue_summary=issue_summary,
            diagnostics_manifest=manifest,
        )

    @staticmethod
    def _summary(*, title: str, report: DoctorReport) -> str:
        failing = [
            check
            for check in report.checks
            if check.status is not DoctorStatus.OK
        ]
        if not failing:
            return f"{title}\n\nDoctor status: ok."
        lines = [title, "", f"Doctor status: {report.worst_status.value}."]
        for check in failing:
            detail = f"- {check.name}: {check.status.value}"
            if check.message:
                detail += f" - {check.message}"
            lines.append(detail)
        return "\n".join(lines)
