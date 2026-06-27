"""Sub-agent exports.

该包保存子 Agent 隔离执行能力，供 `delegate` 工具调用。
"""

from .delegation import (
    DEFAULT_SUBAGENT_MAX_ITERATIONS,
    DEFAULT_SUBAGENT_TOOLS,
    SUBAGENT_FORBIDDEN_TOOLS,
    SUBAGENT_SYSTEM_PROMPT,
    SubAgentResult,
    SubAgentRunner,
)

__all__ = [
    "DEFAULT_SUBAGENT_MAX_ITERATIONS",
    "DEFAULT_SUBAGENT_TOOLS",
    "SUBAGENT_FORBIDDEN_TOOLS",
    "SUBAGENT_SYSTEM_PROMPT",
    "SubAgentResult",
    "SubAgentRunner",
]

