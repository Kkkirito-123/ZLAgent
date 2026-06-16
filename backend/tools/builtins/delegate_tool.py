"""delegate_tool: hand a complex sub-task off to an isolated sub-agent.

See :mod:`backend.agent.subagents` for the runner. This module is the
thin tool facade that exposes ``delegate(task=...)`` to the LLM and
gates it through the v0.6 confirmation flow.

Why is delegate confirm-tier? — Two reasons:

1. **Cost transparency.** The sub-agent has its own iteration budget;
   approving the delegate means agreeing to spend that budget. Putting
   it behind a yes/no makes the user aware.
2. **Surprise prevention.** A delegate often kicks off a multi-second
   work chain. Confirming first guarantees the user stays oriented; the
   bot doesn't disappear into "thinking" with no user-visible signal.

The tool is **not** lazy: the moment the user says yes, the sub-agent
runs synchronously inside the loop and returns its result. There is no
async fire-and-forget here (unlike the v0.9 review fork) — the parent
agent NEEDS the result to continue its turn.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from ..base import Tool, ToolPermission, ToolResult

if TYPE_CHECKING:
    from ...agent.loop import AgentLoop


class DelegateTool(Tool):
    name = "delegate"
    description = (
        "Hand off a self-contained sub-task to an isolated sub-agent."
        " The sub-agent runs in its own context window and returns one"
        " final text answer to you; its intermediate tool calls do not"
        " enter your context (saving you tokens for the rest of the"
        " conversation).\n\n"
        "**When to delegate** — the user asked for something that takes"
        " many tool calls and lots of data:\n"
        "* '研究 X 然后写报告' / '看这 N 个网页对比一下'\n"
        "* '处理这个 csv，按 Y 字段分组求平均'\n"
        "* '帮我列今天 hacker news 前 10 条挑最有意思的'\n\n"
        "**When NOT to delegate**:\n"
        "* Single-step lookups — just call the tool directly.\n"
        "* Anything that needs to mutate state (cron / skill / memory)"
        " — the sub-agent is barred from those tools by design.\n\n"
        "Permission tier is **confirm**: the operator sees your task"
        " description and approves before the sub-agent runs. Pick a"
        " ``task`` string that's specific enough the user knows what"
        " they're approving (NOT '帮我搞定那个事' — say what 'that' is)."
    )
    permission = ToolPermission.CONFIRM
    is_read_only = False
    is_concurrency_safe = False
    is_destructive = False
    max_result_chars = 8_000
    search_hint = "delegate subagent research long task isolated context"
    should_defer = True
    parameters_schema = {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "minLength": 4,
                "description": (
                    "Self-contained instruction for the sub-agent."
                    " Should describe the goal, the inputs (URLs / file"
                    " paths), and the desired output format. The sub-"
                    "agent does NOT see your conversation history, so"
                    " everything it needs must be in this string."
                ),
            },
            "max_iterations": {
                "type": "integer",
                "minimum": 1, "maximum": 30,
                "default": 12,
                "description": (
                    "Tool-call budget for the sub-agent. Default 12 is"
                    " plenty for fetch+parse+write chains. Bump only"
                    " when you genuinely need a larger crawl; each"
                    " iteration is one LLM round-trip plus tool runs."
                ),
            },
            "allowed_tools": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional subset of the default tool whitelist"
                    " (read_url / web_search / read_file / write_file /"
                    " code_execution). When supplied, only these tools"
                    " are exposed to the sub-agent. Forbidden tools"
                    " (skill_manage / cron_manage / memory_manage /"
                    " delegate) are filtered regardless."
                ),
            },
        },
        "required": ["task"],
    }

    def __init__(self, agent_loop: "AgentLoop") -> None:
        self._agent_loop = agent_loop

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        # Local import keeps tools/builtins/__init__ from importing the
        # whole agent layer at module load.
        from ...agent.subagents import (
            DEFAULT_SUBAGENT_MAX_ITERATIONS,
            SubAgentRunner,
        )

        task = arguments.get("task")
        if not isinstance(task, str) or len(task.strip()) < 4:
            return ToolResult(
                ok=False, content="",
                error="task must be a non-empty string (≥4 chars)",
            )

        max_iter_raw = arguments.get("max_iterations")
        try:
            max_iter = int(max_iter_raw) if max_iter_raw is not None else DEFAULT_SUBAGENT_MAX_ITERATIONS
        except (TypeError, ValueError):
            return ToolResult(
                ok=False, content="", error="max_iterations must be int",
            )
        if not 1 <= max_iter <= 30:
            return ToolResult(
                ok=False, content="", error="max_iterations must be between 1 and 30",
            )

        allowed = arguments.get("allowed_tools")
        if allowed is not None:
            if not isinstance(allowed, list) or any(not isinstance(t, str) for t in allowed):
                return ToolResult(
                    ok=False, content="",
                    error="allowed_tools must be a list of tool-name strings",
                )

        runner = SubAgentRunner(
            self._agent_loop,
            allowed_tools=allowed,
            max_iterations=max_iter,
        )
        result = await runner.run(task)

        # Surface the sub-agent's final text either as the tool result
        # body (success) or as a structured error (failure). Either way
        # we annotate with the tool-call count so the parent LLM sees
        # how much work happened.
        suffix = (
            f"\n\n[delegate] sub-agent ran {result.tool_call_count} tool"
            f" call(s); invoked={list(result.invoked_tools)}"
        )
        if result.ok:
            return ToolResult(ok=True, content=result.final_text + suffix)
        return ToolResult(
            ok=False,
            content=(result.final_text or "") + suffix,
            error=result.error or "sub-agent failed without a specific reason",
        )
