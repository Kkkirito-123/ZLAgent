"""Tool / phase → user-facing ping label mapping.

This module is plain data; importing it has no side effects. The
:class:`ProgressEmitter` consults these tables to decide what to say
(and whether to say anything) when a tool starts or a turn enters a
named phase.

Conventions:
- Labels are short Chinese strings with a single leading emoji.
- ``None`` means "stay silent" for that tool / phase — used for
  ultra-internal tools (``tool_search``) and tools whose name is too
  noisy to surface (``delegate``).
- MCP tools always carry the ``mcp__<server>__<tool>`` prefix; the
  emitter falls back to a generic label so install-new MCP servers
  light up automatically without needing to update this map.
"""
from __future__ import annotations

from typing import Optional


# Per-tool labels. Missing names ⇒ no ping (silent).
# v0.45 — slashed the noisy bookkeeping pings (memory / knowledge /
# wiki / skill_manage / cron_manage / mcp_manage). Users complained
# they were getting 5 acks per turn ("整理记忆" / "配置 MCP" / ...) for
# operations that complete in <300 ms and they didn't ask about. Only
# the genuinely slow + user-visible network/compute tools keep a label.
TOOL_LABELS: dict[str, Optional[str]] = {
    # web / fetch — slow, user-visible
    "web_search": "🔎 在网上找资料…",
    "read_url": "📖 读取网页中…",
    # compute — slow, user-visible
    "code_execution": "🧮 计算中…",
    # memory / knowledge / wiki — silent: these are bookkeeping ops
    # the user did not request and that complete instantly.
    "memory_manage": None,
    "knowledge_ingest": None,
    "knowledge_inspect": None,
    "knowledge_mode_manage": None,
    "wiki_search": None,
    "wiki_decay": None,
    "wiki_manage": None,
    # skills / cron / mcp — silent: "setting it up" is the user's own
    # ask, no need to announce "setting up" mid-execution.
    "skill_manage": None,
    "cron_manage": None,
    "mcp_manage": None,
    # file IO
    "read_file": None,
    "write_file": "✍️ 写入文件…",
    "list_directory": None,
    # routing / planning helpers — too internal to surface
    "tool_search": None,
    "delegate": None,
    # travel domain — slow due to upstream HTTP
    "travel_realtime": "🛫 查实时旅行数据…",
    "visited_map": None,
}

# Generic fallback for MCP tools — every ``mcp__server__tool`` lights
# up as a "calling MCP" ping the first time per turn.
MCP_GENERIC_LABEL = "🔌 调用 MCP 工具…"

# Phase labels. v0.45+: opening thinking ack silenced — users found
# "🤔 想想这事儿…" anthropomorphic and noisy, and it cannot be
# suppressed by the LLM because it fires before the LLM speaks.
# Tool pings ("🔎 在网上找资料…", "📖 读取网页中…", etc.) still cover
# the genuinely slow operations the user actually cares about.
PHASE_LABELS: dict[str, Optional[str]] = {
    "starting": None,
}


def tool_to_text(tool_name: str) -> Optional[str]:
    """Return the user-facing ping label for a tool, or ``None`` if silent."""
    if not tool_name:
        return None
    if tool_name in TOOL_LABELS:
        return TOOL_LABELS[tool_name]
    # MCP tools follow the ``mcp__<server>__<tool>`` naming convention
    # established by the MCP client subsystem.
    if tool_name.startswith("mcp__"):
        return MCP_GENERIC_LABEL
    return None


def phase_to_text(phase: str) -> Optional[str]:
    """Return the user-facing label for a turn phase, or ``None``."""
    return PHASE_LABELS.get(phase)


__all__ = [
    "MCP_GENERIC_LABEL",
    "PHASE_LABELS",
    "TOOL_LABELS",
    "phase_to_text",
    "tool_to_text",
]
