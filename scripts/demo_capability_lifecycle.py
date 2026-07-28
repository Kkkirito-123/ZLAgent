#!/usr/bin/env python3
"""Offline proof of ZLAgent's Skill + MCP capability lifecycle.

The demo deliberately uses a local temporary workspace and an in-process MCP
manager fake. It proves the harness boundary and metadata contract without
claiming that a real third-party MCP transport or provider was exercised.
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import tempfile
from typing import Any, Optional


ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.harness import HarnessExecution, HarnessFacade  # noqa: E402
from backend.harness.observability.tracer import TraceRecorder  # noqa: E402
from backend.mcp.config import MCPServerConfig  # noqa: E402
from backend.mcp.connection import DiscoveredTool  # noqa: E402
from backend.mcp.tool_wrapper import build_mcp_tools  # noqa: E402
from backend.skills.loader import SkillLoader  # noqa: E402
from backend.tools import ToolRegistry  # noqa: E402
from backend.tools.builtins.skill_manage import SkillManageTool  # noqa: E402


SKILL_NAME = "harness-notes"
MCP_TOOL_NAME = "mcp__mock__echo"


class MockMCPManager:
    """Small manager-shaped fake for an offline, deterministic MCP call."""

    def __init__(self) -> None:
        self._config = MCPServerConfig(
            name="mock",
            command="offline-mock",
            tool_override_permission={"echo": "safe"},
            description="Offline read-only MCP fixture.",
        )
        self._descriptor = DiscoveredTool(
            server_name="mock",
            name="echo",
            description="Return the supplied text without external I/O.",
            input_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        )

    def list_tools(self) -> list[DiscoveredTool]:
        return [self._descriptor]

    def get_config(self, server_name: str) -> Optional[MCPServerConfig]:
        return self._config if server_name == self._config.name else None

    async def call(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any] | None,
    ) -> tuple[bool, str, Optional[str]]:
        if server_name != "mock" or tool_name != "echo":
            return False, "", "unknown mock MCP tool"
        text = str((arguments or {}).get("text") or "")
        return True, f"mock echo: {text}", None


async def run_demo(workspace_root: pathlib.Path) -> dict[str, Any]:
    """Run the lifecycle and return JSON-serializable evidence."""
    skills_root = workspace_root / "skills"
    registry = ToolRegistry()
    registry.register(SkillManageTool(skills_root))
    tracer = TraceRecorder()
    execution = HarnessExecution(registry, tracer=tracer)

    create_args = {
        "action": "create",
        "skill_name": SKILL_NAME,
        "skill_description": "Record evidence-backed Harness learning notes.",
        "skill_body": (
            "# Harness notes\n\n"
            "For each capability, record permission, evidence, side effects, "
            "and remaining live-validation gaps."
        ),
        "tags": ["harness", "learning"],
    }
    blocked = await execution.execute("skill_manage", create_args)
    if blocked.status.value != "requires_confirmation":
        raise RuntimeError("skill creation bypassed the confirmation boundary")
    if (skills_root / SKILL_NAME).exists():
        raise RuntimeError("blocked skill creation changed the workspace")

    created = await execution.execute(
        "skill_manage",
        create_args,
        allow_confirm=True,
        session_id="demo:capability-lifecycle",
    )
    if not created.ok or not created.side_effects:
        raise RuntimeError(created.error or "skill creation lacked side-effect evidence")

    skill_loader = SkillLoader(skills_root)
    loaded_skills = skill_loader.load()
    if SKILL_NAME not in loaded_skills:
        raise RuntimeError("created skill was not discoverable")

    manager = MockMCPManager()
    mcp_tools = build_mcp_tools(manager)
    registry.register_many(mcp_tools)
    harness = HarnessFacade(
        tool_registry=registry,
        tool_execution=execution,
        skill_loader=skill_loader,
        mcp_manager=manager,
    )
    inventory_before = harness.inventory().to_dict()
    visible_skills = {item["id"] for item in inventory_before["skills"]}
    visible_tools = {item["name"] for item in inventory_before["tools"]}
    if SKILL_NAME not in visible_skills or MCP_TOOL_NAME not in visible_tools:
        raise RuntimeError("installed capabilities were missing from inventory")

    called = await execution.execute(
        MCP_TOOL_NAME,
        {"text": "permission -> execution -> evidence"},
        session_id="demo:capability-lifecycle",
    )
    if not called.ok or not called.evidence:
        raise RuntimeError(called.error or "MCP result lacked evidence")

    removed = await harness.uninstall_skill(SKILL_NAME)
    inventory_after = harness.inventory().to_dict()
    remaining_skills = {item["id"] for item in inventory_after["skills"]}
    if not removed.ok or SKILL_NAME in remaining_skills:
        raise RuntimeError(removed.message or "skill uninstall did not refresh inventory")

    return {
        "confirmation": {
            "status": blocked.status.value,
            "workspace_changed": False,
        },
        "skill": {
            "name": SKILL_NAME,
            "created": created.ok,
            "side_effects": [item.to_dict() for item in created.side_effects],
            "visible_before_uninstall": True,
        },
        "mcp": {
            "tool": MCP_TOOL_NAME,
            "permission": mcp_tools[0].permission.value,
            "content": called.content,
            "source": called.source,
            "evidence": [item.to_dict() for item in called.evidence],
        },
        "uninstall": {
            "ok": removed.ok,
            "skill_visible_after_uninstall": False,
        },
        "trace": tracer.recent_tools(10),
        "limits": [
            "MCP transport is mocked and offline.",
            "No external provider, credential, network, or durable task ledger is exercised.",
        ],
    }


async def _main() -> None:
    with tempfile.TemporaryDirectory(prefix="zlagent-capability-demo-") as tmp:
        result = await run_demo(pathlib.Path(tmp))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(_main())
