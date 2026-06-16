"""Harness 协调层。

Harness 是围绕 Agent Loop 的运行支撑系统。本包只放通用协调件：
门面、扩展清单、生命周期、观测、进度和命令入口。具体能力仍由
skills、memory、tools、mcp、cron 等独立包实现。
"""

from .extensions.inventory import HarnessInventory, build_inventory
from .facade import (
    Harness,
    HarnessFacade,
    HarnessRuntime,
    build_harness,
    build_harness_facade,
)

__all__ = [
    "Harness",
    "HarnessFacade",
    "HarnessInventory",
    "HarnessRuntime",
    "build_harness",
    "build_harness_facade",
    "build_inventory",
]
