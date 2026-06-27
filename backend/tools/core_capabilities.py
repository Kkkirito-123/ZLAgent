from __future__ import annotations

from typing import Any, Optional

from .base import Tool
from .builtins import CronManageTool, ReadUrlTool, WebSearchTool
from .registry import ToolRegistry

CORE_REMINDER_TOOL_NAMES = frozenset({"cron_manage"})
CORE_WEB_QUERY_TOOL_NAMES = frozenset({"read_url", "web_search"})
CORE_ASSISTANT_TOOL_NAMES = CORE_REMINDER_TOOL_NAMES | CORE_WEB_QUERY_TOOL_NAMES


def core_web_query_tools() -> list[Tool]:
    return [ReadUrlTool(), WebSearchTool()]


def core_reminder_tools(*, skill_loader: Optional[Any] = None) -> list[Tool]:
    # v0.45 — passing the SkillLoader lets CronManageTool reject a
    # ``skill_hint`` that points at a non-existent skill at create
    # time, instead of failing at the next tick.
    return [CronManageTool(skill_loader=skill_loader)]


def core_assistant_tools(*, skill_loader: Optional[Any] = None) -> list[Tool]:
    return [*core_web_query_tools(), *core_reminder_tools(skill_loader=skill_loader)]


def register_core_web_query_tools(registry: ToolRegistry) -> None:
    registry.register_many(core_web_query_tools())


def register_core_reminder_tools(
    registry: ToolRegistry, *, skill_loader: Optional[Any] = None,
) -> None:
    registry.register_many(core_reminder_tools(skill_loader=skill_loader))
