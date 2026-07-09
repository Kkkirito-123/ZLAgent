from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from harness.tools import (  # noqa: E402
    Evidence,
    RecommendedNextAction,
    SideEffect,
    ToolErrorType,
    ToolResult,
    ToolResultStatus,
)


class ToolResultMetadataTests(unittest.TestCase):
    def test_success_keeps_readable_content_and_structured_metadata(self) -> None:
        result = ToolResult.success(
            "read ok",
            raw={"bytes": 8},
            evidence=[
                Evidence(
                    type="file",
                    ref="/tmp/example.txt",
                    summary="file content hash observed",
                    metadata={"sha256": "abc"},
                )
            ],
            side_effects=[
                SideEffect(
                    type="filesystem",
                    target="/tmp/example.txt",
                    risk="none",
                )
            ],
            source="read_file",
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.to_tool_message_content(), "read ok")
        self.assertEqual(result.status, ToolResultStatus.SUCCESS)
        self.assertEqual(result.raw, {"bytes": 8})

        metadata = result.to_metadata()
        self.assertEqual(metadata["status"], "success")
        self.assertEqual(metadata["source"], "read_file")
        self.assertEqual(metadata["evidence"][0]["ref"], "/tmp/example.txt")
        self.assertEqual(metadata["side_effects"][0]["risk"], "none")

    def test_failure_has_error_type_and_recovery_hint(self) -> None:
        result = ToolResult.failure(
            "missing required path",
            error_type=ToolErrorType.INVALID_INPUT,
            recoverable_by_model=True,
            recommended_next_action=RecommendedNextAction.RETRY,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.status, ToolResultStatus.ERROR)
        self.assertEqual(
            result.to_tool_message_content(),
            "[tool error] invalid_input: missing required path",
        )

        metadata = result.to_metadata()
        self.assertEqual(metadata["error_type"], "invalid_input")
        self.assertTrue(metadata["recoverable_by_model"])
        self.assertEqual(metadata["recommended_next_action"], "retry")

    def test_denied_and_requires_confirmation_are_distinct(self) -> None:
        denied = ToolResult.denied("blocked")
        confirm = ToolResult.requires_confirmation("needs user approval")

        self.assertEqual(denied.status, ToolResultStatus.DENIED)
        self.assertEqual(denied.recommended_next_action, RecommendedNextAction.STOP)
        self.assertEqual(
            denied.to_tool_message_content(),
            "[tool denied] permission_denied: blocked",
        )

        self.assertEqual(confirm.status, ToolResultStatus.REQUIRES_CONFIRMATION)
        self.assertEqual(confirm.recommended_next_action, RecommendedNextAction.ASK_USER)
        self.assertEqual(
            confirm.to_tool_message_content(),
            "[tool requires_confirmation] permission_denied: needs user approval",
        )

    def test_partial_result_is_successful_but_statused(self) -> None:
        result = ToolResult.partial("some rows returned", error="truncated")

        self.assertTrue(result.ok)
        self.assertEqual(result.status, ToolResultStatus.PARTIAL)
        self.assertEqual(result.to_tool_message_content(), "some rows returned")


if __name__ == "__main__":
    unittest.main()
