from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from re_zlagent.harness.tools import PermissionDecision, PermissionPolicy, Tool, ToolPermission  # noqa: E402


class SafeTool(Tool):
    name = "safe_tool"
    permission = ToolPermission.SAFE


class ConfirmReadTool(Tool):
    name = "confirm_read_tool"
    permission = ToolPermission.CONFIRM
    is_read_only = True


class ConfirmMutationTool(Tool):
    name = "confirm_mutation_tool"
    permission = ToolPermission.CONFIRM
    is_read_only = False


class DeniedTool(Tool):
    name = "denied_tool"
    permission = ToolPermission.DENY


class PermissionPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = PermissionPolicy()

    def test_safe_tool_is_allowed(self) -> None:
        result = self.policy.decide_tool_call(
            tool=SafeTool(),
            tool_name="safe_tool",
        )

        self.assertEqual(result.decision, PermissionDecision.ALLOW)

    def test_denied_tool_is_denied(self) -> None:
        result = self.policy.decide_tool_call(
            tool=DeniedTool(),
            tool_name="denied_tool",
        )

        self.assertEqual(result.decision, PermissionDecision.DENY)
        self.assertIn("denied", result.reason)

    def test_unknown_tool_is_denied(self) -> None:
        result = self.policy.decide_tool_call(
            tool=None,
            tool_name="missing",
        )

        self.assertEqual(result.decision, PermissionDecision.DENY)
        self.assertIn("unknown tool", result.reason)

    def test_confirm_mutation_requires_confirmation_when_interactive(self) -> None:
        result = self.policy.decide_tool_call(
            tool=ConfirmMutationTool(),
            tool_name="confirm_mutation_tool",
            interactive=True,
        )

        self.assertEqual(result.decision, PermissionDecision.CONFIRM)

    def test_confirm_mutation_is_denied_when_non_interactive(self) -> None:
        result = self.policy.decide_tool_call(
            tool=ConfirmMutationTool(),
            tool_name="confirm_mutation_tool",
            interactive=False,
        )

        self.assertEqual(result.decision, PermissionDecision.DENY)
        self.assertIn("non-interactive", result.reason)

    def test_confirm_read_only_action_is_allowed(self) -> None:
        result = self.policy.decide_tool_call(
            tool=ConfirmReadTool(),
            tool_name="confirm_read_tool",
        )

        self.assertEqual(result.decision, PermissionDecision.ALLOW)
        self.assertTrue(result.allow_confirm)
        self.assertTrue(result.read_only_action)

    def test_trusted_confirm_tool_is_allowed(self) -> None:
        result = self.policy.decide_tool_call(
            tool=ConfirmMutationTool(),
            tool_name="confirm_mutation_tool",
            trust_confirm_tools=True,
        )

        self.assertEqual(result.decision, PermissionDecision.ALLOW)
        self.assertTrue(result.trusted)


if __name__ == "__main__":
    unittest.main()
