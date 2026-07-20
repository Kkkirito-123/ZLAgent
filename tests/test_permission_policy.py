from __future__ import annotations

import unittest

from backend.tools import Tool, ToolPermission
from backend.tools.builtins.knowledge_ingest import KnowledgeIngestTool
from backend.tools.builtins.knowledge_mode_manage import KnowledgeModeManageTool
from backend.tools.builtins.memory_manage import MemoryManageTool
from backend.tools.builtins.skill_manage import SkillManageTool
from backend.tools.permission import PermissionDecision, PermissionPolicy


class SafeTool(Tool):
    name = "safe"
    permission = ToolPermission.SAFE
    is_read_only = True


class MultiActionTool(Tool):
    name = "multi"
    permission = ToolPermission.CONFIRM
    is_read_only = False

    def is_action_read_only(self, arguments: dict | None) -> bool:
        return (arguments or {}).get("action") == "list"


class PermissionPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = PermissionPolicy()

    def decide(
        self,
        tool: Tool,
        arguments: dict,
        *,
        interactive: bool = True,
        trusted: bool = False,
        platform: str = "weixin",
    ):
        return self.policy.decide_tool_call(
            tool=tool,
            tool_name=tool.name,
            arguments=arguments,
            platform=platform,
            interactive=interactive,
            trust_confirm_tools=trusted,
        )

    def test_safe_and_parameter_level_reads_are_allowed(self) -> None:
        self.assertEqual(
            self.decide(SafeTool(), {}).decision,
            PermissionDecision.ALLOW,
        )
        decision = self.decide(MultiActionTool(), {"action": "list"})
        self.assertEqual(decision.decision, PermissionDecision.ALLOW)
        self.assertTrue(decision.read_only_action)

    def test_mutations_confirm_in_chat_and_deny_in_cron(self) -> None:
        tool = MultiActionTool()
        self.assertEqual(
            self.decide(tool, {"action": "write"}).decision,
            PermissionDecision.CONFIRM,
        )
        self.assertEqual(
            self.decide(tool, {"action": "write"}, interactive=False).decision,
            PermissionDecision.DENY,
        )
        self.assertEqual(
            self.decide(tool, {"action": "write"}, trusted=True).decision,
            PermissionDecision.ALLOW,
        )

    def test_platform_does_not_auto_confirm_mutations(self) -> None:
        tool = MultiActionTool()
        for platform in ("weixin", "webhook", "cron"):
            with self.subTest(platform=platform):
                self.assertEqual(
                    self.decide(
                        tool,
                        {"action": "write"},
                        platform=platform,
                    ).decision,
                    PermissionDecision.CONFIRM,
                )

    def test_durable_builtin_tools_are_confirm_tier(self) -> None:
        for tool_type in (
            SkillManageTool,
            KnowledgeIngestTool,
            KnowledgeModeManageTool,
            MemoryManageTool,
        ):
            with self.subTest(tool=tool_type.__name__):
                self.assertIs(tool_type.permission, ToolPermission.CONFIRM)


if __name__ == "__main__":
    unittest.main()
