from __future__ import annotations

import json
from typing import Any, TYPE_CHECKING

from ...agent.context import current_turn_context
from ..base import Tool, ToolPermission, ToolResult

if TYPE_CHECKING:
    from ..registry import ToolRegistry


_OPENGUI_EXPLICIT_TRIGGER = "请你用手机"
_PHONE_CONTROL_TOOLS = frozenset({"open_gui"})


class ToolSearchTool(Tool):
    name = "tool_search"
    description = (
        "Search the deferred/specialized tool catalog by keyword and return exact"
        " tool names. Use this when you need a tool that is not currently visible"
        " in the tool schema, including MCP tools or rarely used high-cost tools."
        " Phone-control tools such as open_gui are only discoverable when the"
        " current original user message explicitly contains '请你用手机'; app"
        " names or media verbs alone must not activate phone execution."
        " Do not use this for core assistant tools that are already visible"
        " (read_url, web_search, cron_manage). By default it returns deferred"
        " tools only; set include_loaded=true only for operator/debug inspection."
        " After this tool returns, matching deferred tools become available for"
        " the next assistant tool-call step."
        " v1.2.0 — call AT MOST ONCE per turn. If the visible tool schema"
        " already exposes a tool that fits the task (web_search / read_url /"
        " cron_manage / skill_manage / memory_manage / mcp_manage etc.), use"
        " it directly without searching. Repeated tool_search calls within"
        " the same turn waste an LLM round-trip and are an anti-pattern."
    )
    permission = ToolPermission.SAFE
    is_read_only = True
    is_concurrency_safe = True
    is_destructive = False
    always_load = True
    should_defer = False
    search_hint = "search discover tools mcp deferred catalog capabilities"
    parameters_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Keyword query such as 'browser', 'mcp github', 'code execution', or 'cron'.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 20,
                "default": 8,
                "description": "Maximum number of matching tools to return.",
            },
            "include_loaded": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Default false. When false, only deferred tools are returned"
                    " to keep runtime search focused. Set true only when you need"
                    " to inspect already-loaded core tools for debugging."
                ),
            },
        },
        "required": ["query"],
    }

    def __init__(self, registry: "ToolRegistry") -> None:
        self._registry = registry

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        query = str(arguments.get("query") or "").strip()
        if not query:
            return ToolResult(ok=False, content="", error="query is required")
        try:
            limit = int(arguments.get("limit") or 8)
        except (TypeError, ValueError):
            limit = 8
        limit = max(1, min(limit, 20))
        include_loaded = bool(arguments.get("include_loaded") or False)
        allow_phone_tools = _current_turn_allows_phone_tools()
        matches = [
            m for m in self._registry.search(
                query,
                limit=limit,
                include_description=False,
            )
            if (
                m["name"] != self.name
                and (include_loaded or m["deferred"])
                and (m["name"] not in _PHONE_CONTROL_TOOLS or allow_phone_tools)
            )
        ]
        if not matches:
            return ToolResult(
                ok=True,
                content=(
                    f"No deferred tools matched query: {query!r}. Core tools are"
                    " already visible; set include_loaded=true only for inspection."
                ),
                raw={"activate_tools": []},
            )
        lines = [f"Tool search results for: {query}", ""]
        activate: list[str] = []
        for idx, item in enumerate(matches, 1):
            if item["deferred"]:
                activate.append(item["name"])
            deferred = "deferred" if item["deferred"] else "loaded"
            lines.append(f"{idx}. {item['name']} ({item['permission']}, {deferred})")
            if item.get("search_hint"):
                lines.append(f"   hint: {item['search_hint']}")
            desc = item.get("description") or ""
            if desc:
                lines.append(f"   {desc[:240]}")
            lines.append("")
        lines.append("Exact deferred tool names activated for the next step:")
        lines.append(json.dumps(activate, ensure_ascii=False))
        return ToolResult(ok=True, content="\n".join(lines).strip(), raw={"activate_tools": activate})


def _current_turn_allows_phone_tools() -> bool:
    ctx = current_turn_context()
    if ctx is None:
        return False
    return _OPENGUI_EXPLICIT_TRIGGER in (ctx.user_message or "")
