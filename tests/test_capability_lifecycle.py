from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.demo_capability_lifecycle import (
    MCP_TOOL_NAME,
    SKILL_NAME,
    run_demo,
)


class CapabilityLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_skill_and_mock_mcp_cross_the_harness_boundary(self) -> None:
        with tempfile.TemporaryDirectory(prefix="zlagent-capability-test-") as tmp:
            result = await run_demo(Path(tmp))

        self.assertEqual(
            result["confirmation"]["status"],
            "requires_confirmation",
        )
        self.assertFalse(result["confirmation"]["workspace_changed"])

        self.assertEqual(result["skill"]["name"], SKILL_NAME)
        self.assertTrue(result["skill"]["created"])
        self.assertEqual(
            result["skill"]["side_effects"][0]["target"],
            f"skill_name:{SKILL_NAME}",
        )

        self.assertEqual(result["mcp"]["tool"], MCP_TOOL_NAME)
        self.assertEqual(result["mcp"]["permission"], "safe")
        self.assertEqual(result["mcp"]["source"], MCP_TOOL_NAME)
        self.assertEqual(
            result["mcp"]["evidence"][0]["ref"],
            "mcp://mock/echo",
        )

        self.assertTrue(result["uninstall"]["ok"])
        self.assertFalse(result["uninstall"]["skill_visible_after_uninstall"])
        self.assertEqual(
            [item["name"] for item in result["trace"]],
            ["skill_manage", "skill_manage", MCP_TOOL_NAME, "skill_manage"],
        )
        self.assertEqual(result["trace"][2]["evidence_count"], 1)
        self.assertEqual(result["trace"][1]["side_effect_count"], 1)


if __name__ == "__main__":
    unittest.main()
