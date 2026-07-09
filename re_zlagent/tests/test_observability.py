from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from harness.observability import (  # noqa: E402
    DoctorCheck,
    DoctorRunner,
    DoctorStatus,
    InMemoryTraceRecorder,
    SpanStatus,
    SupportBundleBuilder,
    build_harness_doctor,
    check_harness_facade,
    redact_mapping,
)
from harness import build_harness_facade  # noqa: E402
from harness.storage import InMemoryTaskStore  # noqa: E402
from harness.tools import ToolRegistry  # noqa: E402


class TraceRecorderTests(unittest.TestCase):
    def test_start_record_and_end_span(self) -> None:
        recorder = InMemoryTraceRecorder()

        span = recorder.start_span("runtime.run", metadata={"run_id": "run-1"})
        event = recorder.record_event(
            span.id,
            "tool.called",
            metadata={"tool": "read_file"},
        )
        ended = recorder.end_span(span.id, status=SpanStatus.OK)

        loaded = recorder.get_span(span.id)
        self.assertEqual(event.name, "tool.called")
        self.assertEqual(loaded.events[0].metadata["tool"], "read_file")
        self.assertEqual(ended.status, SpanStatus.OK)
        self.assertIsNotNone(ended.duration_ms())

    def test_parent_span_must_exist(self) -> None:
        recorder = InMemoryTraceRecorder()

        with self.assertRaises(ValueError):
            recorder.start_span("child", parent_id="missing")

    def test_cannot_record_event_after_span_ends(self) -> None:
        recorder = InMemoryTraceRecorder()
        span = recorder.start_span("runtime.run")
        recorder.end_span(span.id)

        with self.assertRaises(ValueError):
            recorder.record_event(span.id, "late")

        with self.assertRaises(ValueError):
            recorder.end_span(span.id)

    def test_list_spans_filters_by_trace_id_and_preserves_order(self) -> None:
        recorder = InMemoryTraceRecorder()
        root = recorder.start_span("root", trace_id="trace-1")
        child = recorder.start_span("child", trace_id="trace-1", parent_id=root.id)
        other = recorder.start_span("other", trace_id="trace-2")

        self.assertEqual(
            [span.id for span in recorder.list_spans(trace_id="trace-1")],
            [root.id, child.id],
        )
        self.assertEqual(
            [span.id for span in recorder.list_spans()],
            [root.id, child.id, other.id],
        )

    def test_metadata_is_redacted_recursively(self) -> None:
        recorder = InMemoryTraceRecorder()

        span = recorder.start_span(
            "runtime.run",
            metadata={
                "api_key": "sk-secret",
                "nested": {"token": "abc", "safe": "ok"},
                "items": [{"password": "pw"}],
            },
        )
        recorder.record_event(
            span.id,
            "request",
            metadata={"Authorization": "Bearer token", "path": "/ok"},
        )

        loaded = recorder.get_span(span.id)
        self.assertEqual(loaded.metadata["api_key"], "[REDACTED]")
        self.assertEqual(loaded.metadata["nested"]["token"], "[REDACTED]")
        self.assertEqual(loaded.metadata["nested"]["safe"], "ok")
        self.assertEqual(loaded.metadata["items"][0]["password"], "[REDACTED]")
        self.assertEqual(loaded.events[0].metadata["Authorization"], "[REDACTED]")
        self.assertEqual(loaded.events[0].metadata["path"], "/ok")

    def test_redact_mapping_does_not_mutate_input(self) -> None:
        source = {"token": "abc", "safe": "ok"}

        redacted = redact_mapping(source)

        self.assertEqual(redacted["token"], "[REDACTED]")
        self.assertEqual(source["token"], "abc")


class DoctorTests(unittest.TestCase):
    def test_doctor_report_worst_status(self) -> None:
        runner = DoctorRunner()
        runner.register(
            "config",
            lambda: DoctorCheck(
                name="config",
                status=DoctorStatus.OK,
                metadata={"api_key": "secret"},
            ),
        )
        runner.register(
            "storage",
            lambda: DoctorCheck(
                name="storage",
                status=DoctorStatus.WARN,
                message="using memory adapter",
            ),
        )

        report = runner.run()

        self.assertTrue(report.ok)
        self.assertEqual(report.worst_status, DoctorStatus.WARN)
        self.assertEqual(report.checks[0].metadata["api_key"], "[REDACTED]")

    def test_doctor_exception_becomes_error_check(self) -> None:
        runner = DoctorRunner()

        def broken() -> DoctorCheck:
            raise RuntimeError("boom")

        runner.register("broken", broken)
        report = runner.run()

        self.assertFalse(report.ok)
        self.assertEqual(report.worst_status, DoctorStatus.ERROR)
        self.assertIn("RuntimeError", report.checks[0].message)

    def test_duplicate_doctor_check_is_rejected(self) -> None:
        runner = DoctorRunner()
        runner.register("config", lambda: DoctorCheck("config", DoctorStatus.OK))

        with self.assertRaises(ValueError):
            runner.register("config", lambda: DoctorCheck("config", DoctorStatus.OK))

    def test_support_bundle_redacts_manifest_and_summarizes_failures(self) -> None:
        report = DoctorRunner()
        report.register(
            "storage",
            lambda: DoctorCheck(
                name="storage",
                status=DoctorStatus.ERROR,
                message="database unavailable",
                metadata={"password": "pw"},
            ),
        )
        recorder = InMemoryTraceRecorder()
        span = recorder.start_span("runtime", metadata={"token": "abc"})
        ended = recorder.end_span(span.id, status=SpanStatus.ERROR, error="bad")

        bundle = SupportBundleBuilder().build(
            title="Runtime failure",
            report=report.run(),
            traces=[ended],
            metadata={"api_key": "sk-test"},
        )

        self.assertIn("storage: error", bundle.issue_summary)
        self.assertEqual(
            bundle.diagnostics_manifest["doctor"]["checks"][0]["metadata"]["password"],
            "[REDACTED]",
        )
        self.assertEqual(
            bundle.diagnostics_manifest["traces"][0]["metadata"]["token"],
            "[REDACTED]",
        )
        self.assertEqual(
            bundle.diagnostics_manifest["metadata"]["api_key"],
            "[REDACTED]",
        )

    def test_harness_facade_doctor_reports_missing_required_components(self) -> None:
        check = check_harness_facade(build_harness_facade())

        self.assertEqual(check.status, DoctorStatus.ERROR)
        self.assertIn("tool_registry", check.message)
        self.assertIn("task_store", check.message)

    def test_harness_doctor_reports_ready_facade(self) -> None:
        facade = build_harness_facade(
            tool_registry=ToolRegistry(),
            task_store=InMemoryTaskStore(),
        )

        report = build_harness_doctor(facade).run()

        self.assertTrue(report.ok)
        self.assertEqual(report.checks[0].status, DoctorStatus.OK)
        self.assertTrue(report.checks[0].metadata["components"]["tool_registry"])


if __name__ == "__main__":
    unittest.main()
