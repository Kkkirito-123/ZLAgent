"""Agent loop and session orchestration package.

该包以 `loop.py` 作为 Agent 单轮执行入口，其余能力按职责放入子包。
历史模块名通过 `sys.modules` 做兼容映射，文件系统中无需继续保留零散兼容文件。
"""

from __future__ import annotations

import sys

from . import cron_runner as _cron_runner
from . import prompts as _prompts
from .confirmation import formatting as _confirmation_formatting

sys.modules.setdefault(__name__ + ".cron", _cron_runner)
sys.modules.setdefault(__name__ + ".loop_prompts", _prompts)
sys.modules.setdefault(__name__ + ".loop_confirm", _confirmation_formatting)

