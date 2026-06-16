"""Tool-loop budget helpers.

该模块保存单轮工具调用预算的启发式规则。`AgentLoop` 只调用这里的函数，
避免在主循环文件中继续放置文本匹配规则。
"""

from __future__ import annotations

import re


_LIST_TASK_PATTERNS = (
    re.compile(
        r"(?:找|给我|列|推荐).{0,6}"
        r"(?:\d+|几|多|[一二两三四五六七八九十])\s*"
        r"(?:篇|个|条|项|本)"
    ),
    re.compile(r"(?:综述|对比|比较|SOTA|顶刊|顶会|state[- ]of[- ]the[- ]art)", re.IGNORECASE),
    re.compile(r"(?:survey|compare|find\s+\d+\s+\w+)", re.IGNORECASE),
)


def adaptive_tool_budget(user_text: str, *, base: int, list_task: int) -> int:
    """根据用户文本判断单轮工具调用预算。

    `user_text` 是本轮输入文本，`base` 是普通任务预算，`list_task` 是列表收集类
    任务预算。命中论文、对比、综述等列表收集类表达时返回 `list_task`，其余情况
    返回 `base`。
    """

    text = (user_text or "").strip()
    if not text or list_task <= base:
        return base
    for pattern in _LIST_TASK_PATTERNS:
        if pattern.search(text):
            return list_task
    return base
