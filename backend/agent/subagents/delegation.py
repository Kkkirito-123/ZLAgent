"""Sub-agent runner for the v0.13 ``delegate_tool``.

A *delegated turn* is a fully isolated execution of the agent loop that
runs to completion in its own context window and returns ONE final
string back to the parent turn. This is the standard "task isolation"
pattern Hermes uses for ``delegate_tool``: the parent agent's prompt
budget never sees the sub-agent's intermediate tool calls — only the
sub-agent's final summary.

Why bother
----------

Without delegation, a multi-step research task ("帮我看下今天 cs.AI
前 5 篇论文挑最有意思的写中文报告") forces the parent agent to
accumulate every ``read_url`` result in its own history. After a few
queries the parent's context fills with raw HTML, leaving no room for
follow-up turns. Delegation moves all that bulk into a sub-agent's
private history; the parent only sees the 1-2 KB final report.

Hard constraints (deliberately conservative)
--------------------------------------------

The sub-agent runs with a **restricted toolset** by default. We refuse:

* ``skill_manage`` / ``cron_manage`` / ``memory_manage`` — these
  *mutate state* the user expects to control; a sub-agent silently
  creating a cron or writing a memory mid-turn would surprise the user.
* ``delegate`` itself — no recursion. One delegate per turn, no more.

Read-only / compute tools (``read_url``, ``web_search``, ``read_file``,
``code_execution``) are fair game. Write tools (``write_file``) are
allowed because the parent may legitimately delegate "produce report.md"
— but the sub-agent runs ``interactive=False`` so it can't suspend on a
``write_file`` confirmation; the parent already accepted the delegation
that explicitly mentioned writing files.

Iteration budget
----------------

Sub-agent gets its own (typically smaller) ``max_iterations`` so a
runaway delegation doesn't burn the entire main process budget. Default
is 12 — enough for "fetch + parse + write" type chains but not enough
for an unbounded multi-page research crawl. The parent tool exposes
this as a parameter so the LLM can override per-call.

Failures
--------

The sub-agent's ``LoopOutcome`` is collapsed into either:
* ``(ok=True, final_text)`` — the sub-agent emitted plain assistant
  content
* ``(ok=False, error)`` — the sub-agent exceeded its iteration budget,
  the LLM call failed, or it tried to invoke a forbidden tool

Either way nothing crashes into the parent.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional, Sequence

from loguru import logger

from ...llm import LLMMessage
from ...memory.manager import sanitize_untrusted

if TYPE_CHECKING:
    from ..loop import AgentLoop
    from ..runtime import LoopOutcome


# Tools the sub-agent may invoke. The set is deliberately tight —
# expanding it requires conscious thought because a sub-agent that can
# write to a cron job is no longer "task isolation".
DEFAULT_SUBAGENT_TOOLS = frozenset({
    "read_url",
    "web_search",
    "read_file",
    "write_file",
    "code_execution",
})

# Tools always refused, regardless of caller-supplied whitelists.
SUBAGENT_FORBIDDEN_TOOLS = frozenset({
    "skill_manage",
    "cron_manage",
    "knowledge_ingest",
    "memory_manage",
    "send_message",
    "delegate",
})

DEFAULT_SUBAGENT_MAX_ITERATIONS = 12

SUBAGENT_SYSTEM_PROMPT = (
    "你是 ZLAgent 的子任务 agent。父 agent 把一个**具体子任务**委托给你独立完成。"
    "你的目标只有一个：完成父 agent 给你的任务，最后用一段简洁的文字回答。\n\n"
    "## 可用工具（只这些）\n"
    "- read_url / web_search：拉外部信息\n"
    "- read_file / write_file：在 workspace 里读写文件\n"
    "- code_execution：运行临时 Python / shell 脚本\n\n"
    "## 不能做的事\n"
    "- 不能调用 skill_manage / cron_manage / memory_manage —— 这些是状态变更"
    "工具，应由用户在主对话里决定。\n"
    "- 不能再 delegate（不嵌套子 agent）。\n"
    "- 不需要要求用户确认 —— 你的 toolset 都是 safe 级，自动跑。\n\n"
    "## 输出要求\n"
    "- 完成任务后，回一段文本作为最终结果，不要附带元数据/进度报告。\n"
    "- 如果中途确定任务无法完成，简洁说明原因 + 你尝试过的步骤。\n"
    "- 父 agent 不会看到你的工具调用历史，只会看到这段最终文本。"
)


class SubAgentResult:
    """Tiny value type returned to ``delegate_tool``."""

    __slots__ = ("ok", "final_text", "error", "tool_call_count", "invoked_tools")

    def __init__(
        self,
        *,
        ok: bool,
        final_text: str,
        error: Optional[str] = None,
        tool_call_count: int = 0,
        invoked_tools: tuple[str, ...] = (),
    ) -> None:
        self.ok = ok
        self.final_text = final_text
        self.error = error
        self.tool_call_count = tool_call_count
        self.invoked_tools = invoked_tools


class SubAgentRunner:
    """Drive a one-shot delegated turn against the parent's AgentLoop.

    Stateless: callers construct one per delegate invocation. The
    parent ``AgentLoop`` is referenced (not copied) so the sub-agent
    shares the LLM client, tool registry, and skill loader — only the
    *history* and *iteration budget* are private.
    """

    def __init__(
        self,
        parent_loop: "AgentLoop",
        *,
        allowed_tools: Optional[Sequence[str]] = None,
        max_iterations: int = DEFAULT_SUBAGENT_MAX_ITERATIONS,
    ) -> None:
        self._parent = parent_loop
        # Compute the effective whitelist: caller-supplied (if any),
        # intersected with the always-allowed default set, then
        # filtered by the never-allowed set.
        if allowed_tools is None:
            requested = DEFAULT_SUBAGENT_TOOLS
        else:
            requested = {str(t).strip() for t in allowed_tools if str(t).strip()}
        effective = (set(requested) & set(DEFAULT_SUBAGENT_TOOLS)) - SUBAGENT_FORBIDDEN_TOOLS
        self._allowed_tools = frozenset(effective) or DEFAULT_SUBAGENT_TOOLS
        self._max_iterations = max(1, min(int(max_iterations), 30))

    @property
    def allowed_tools(self) -> frozenset[str]:
        return self._allowed_tools

    @staticmethod
    def _action_filter(tool_name: str, arguments: dict) -> Optional[str]:
        """Last-resort defence: refuse forbidden tools even if the
        whitelist somehow leaked one through.

        Belt-and-braces with the ``tool_whitelist`` arg passed to
        ``_run_tool_loop`` — the whitelist removes the tool from the
        LLM's schema entirely, so the LLM should never even ask for it,
        but a hostile / misbehaving tool could in theory be force-called
        by a future code path. This filter catches that.
        """
        if tool_name in SUBAGENT_FORBIDDEN_TOOLS:
            return (
                f"tool {tool_name!r} is forbidden inside a delegated"
                " sub-agent; only safe / read-only tools are exposed."
            )
        return None

    async def run(self, task: str) -> SubAgentResult:
        """Execute the delegated task and collapse the outcome."""
        cleaned_task = sanitize_untrusted((task or "").strip())
        if not cleaned_task:
            return SubAgentResult(
                ok=False,
                final_text="",
                error="delegated task is empty",
            )
        if self._parent._llm is None or not self._parent.llm_configured:
            return SubAgentResult(
                ok=False,
                final_text="",
                error="LLM not configured; sub-agent cannot run",
            )

        history: list[LLMMessage] = [
            LLMMessage(role="system", content=SUBAGENT_SYSTEM_PROMPT),
            LLMMessage(role="user", content=cleaned_task),
        ]
        logger.info(
            "[delegate] sub-agent starting (tools={}, max_iterations={})",
            sorted(self._allowed_tools), self._max_iterations,
        )
        try:
            outcome: "LoopOutcome" = await self._parent._run_tool_loop(
                history,
                system=SUBAGENT_SYSTEM_PROMPT,
                interactive=False,
                platform="delegate",
                user_id="sub-agent",
                reply_target=None,
                user_text_for_fallback=cleaned_task,
                tool_whitelist=set(self._allowed_tools),
                trust_confirm_tools=False,  # never let the sub-agent bypass IM gates
                max_iterations=self._max_iterations,
                action_filter=self._action_filter,
            )
        except Exception as exc:  # noqa: BLE001 - guard the parent
            logger.exception("[delegate] sub-agent crashed: {}", exc)
            return SubAgentResult(
                ok=False, final_text="",
                error=f"sub-agent crashed: {type(exc).__name__}: {exc}",
            )

        # Collapse outcome → SubAgentResult. v0.45 — use the structured
        # ``failure_kind`` marker rather than string-matching on
        # user-facing text so the parent agent gets a stable signal.
        text = (outcome.final_text or "").strip()
        if not text:
            return SubAgentResult(
                ok=False,
                final_text="",
                error="sub-agent produced no final text",
                tool_call_count=outcome.tool_call_count,
                invoked_tools=outcome.invoked_tool_names,
            )
        if outcome.failure_kind:
            return SubAgentResult(
                ok=False,
                final_text=text,
                error=f"sub-agent terminated: {outcome.failure_kind}",
                tool_call_count=outcome.tool_call_count,
                invoked_tools=outcome.invoked_tool_names,
            )
        logger.info(
            "[delegate] sub-agent done — {} tool call(s); {} chars",
            outcome.tool_call_count, len(text),
        )
        return SubAgentResult(
            ok=True,
            final_text=text,
            tool_call_count=outcome.tool_call_count,
            invoked_tools=outcome.invoked_tool_names,
        )
