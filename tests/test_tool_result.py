from __future__ import annotations

import unittest

from backend.tools import (
    Evidence,
    RecommendedNextAction,
    SideEffect,
    ToolErrorType,
    ToolResult,
    ToolResultStatus,
)


class ToolResultTests(unittest.TestCase):
    def test_success_metadata_is_machine_readable(self) -> None:
        result = ToolResult(
            ok=True,
            content="done",
            source="write_file",
            evidence=(Evidence(type="path", ref="notes/a.md"),),
            side_effects=(
                SideEffect(type="file.write", target="notes/a.md", risk="medium"),
            ),
        )

        self.assertEqual(result.status, ToolResultStatus.SUCCESS)
        self.assertEqual(
            result.to_metadata()["side_effects"][0]["target"],
            "notes/a.md",
        )

    def test_failure_and_confirmation_have_structured_recovery(self) -> None:
        failure = ToolResult.failure(
            "network unavailable",
            error_type=ToolErrorType.EXTERNAL_UNAVAILABLE,
            recoverable_by_model=True,
            recommended_next_action=RecommendedNextAction.USE_ALTERNATIVE_TOOL,
        )
        confirmation = ToolResult.requires_confirmation("approval required")

        self.assertEqual(failure.status, ToolResultStatus.ERROR)
        self.assertIn("external_unavailable", failure.to_tool_message_content())
        self.assertEqual(
            confirmation.status,
            ToolResultStatus.REQUIRES_CONFIRMATION,
        )
        self.assertEqual(
            confirmation.recommended_next_action,
            RecommendedNextAction.ASK_USER,
        )


if __name__ == "__main__":
    unittest.main()
