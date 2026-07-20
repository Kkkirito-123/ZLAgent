"""Harness 门面。

Harness 是围绕 Agent Loop 的整套支撑系统，包含 skills、memory、
tools、MCP、cron、权限、观测和进度等能力。本文件只实现对外门面：
为 FastAPI、IM 命令和后续控制台提供统一的状态读取与管理入口。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .extensions.inventory import HarnessInventory, build_inventory


@dataclass(slots=True)
class HarnessRuntime:
    """提供给 API 与界面的运行时快照。"""

    core: dict[str, Any] = field(default_factory=dict)
    services: dict[str, Any] = field(default_factory=dict)
    extensions: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转换为可 JSON 序列化的字典。"""
        return {
            "core": dict(self.core),
            "services": dict(self.services),
            "extensions": dict(self.extensions),
        }


@dataclass(slots=True)
class HarnessFacade:
    """Harness 系统的统一管理门面。

    该类不拥有具体业务能力，只持有各支撑系统的引用，负责汇总状态、
    转发生命周期操作和处理 IM 控制命令。
    """

    tool_registry: Optional[Any] = None
    tool_execution: Optional[Any] = None
    skill_loader: Optional[Any] = None
    mcp_store: Optional[Any] = None
    mcp_manager: Optional[Any] = None
    mcp_lifecycle: Optional[Any] = None
    plugin_loader: Optional[Any] = None
    tool_memo: Optional[Any] = None
    tracer: Optional[Any] = None
    progress: Optional[Any] = None
    daily_review_service: Optional[Any] = None

    def inventory(self) -> HarnessInventory:
        """返回面向用户的扩展清单。"""
        return build_inventory(
            tool_registry=self.tool_registry,
            skill_loader=self.skill_loader,
            mcp_store=self.mcp_store,
            mcp_manager=self.mcp_manager,
            plugin_loader=self.plugin_loader,
        )

    def runtime(self) -> HarnessRuntime:
        """返回简要运行时状态。"""
        inventory = self.inventory().to_dict()
        return HarnessRuntime(
            core={
                "tool_registry": self.tool_registry is not None,
                "tool_execution": self.tool_execution is not None,
                "tool_memo": self.tool_memo is not None,
                "tracer": self.tracer is not None,
                "progress": self.progress is not None,
            },
            services={
                "skill_loader": self.skill_loader is not None,
                "mcp_store": self.mcp_store is not None,
                "mcp_manager": self.mcp_manager is not None,
                "mcp_lifecycle": self.mcp_lifecycle is not None,
                "plugin_loader": self.plugin_loader is not None,
                "daily_review_service": self.daily_review_service is not None,
            },
            extensions={
                "skills_items": list(inventory.get("skills", [])),
                "mcps_items": list(inventory.get("mcps", [])),
                "plugins_items": list(inventory.get("plugins", [])),
                "tools_items": list(inventory.get("tools", [])),
                "counts": dict(inventory.get("counts", {})),
                "core_summary": dict(inventory.get("core_summary", {})),
            },
        )

    async def uninstall_skill(self, name: str):
        """转发 skill 卸载请求。"""
        from .extensions.lifecycle import uninstall_skill

        return await uninstall_skill(self, name)

    async def uninstall_mcp(self, name: str):
        """转发 MCP 卸载请求。"""
        from .extensions.lifecycle import uninstall_mcp

        return await uninstall_mcp(self, name)

    async def uninstall_plugin(self, name: str):
        """转发插件卸载请求。"""
        from .extensions.lifecycle import uninstall_plugin

        return await uninstall_plugin(self, name)

    async def handle_command(self, text: str) -> Optional[str]:
        """处理 IM 中的 Harness 控制命令。"""
        from .operations.commands import handle_command

        return await handle_command(text, self)


Harness = HarnessFacade


def build_harness_facade(
    *,
    tool_registry: Optional[Any] = None,
    skill_loader: Optional[Any] = None,
    mcp_store: Optional[Any] = None,
    mcp_manager: Optional[Any] = None,
    mcp_lifecycle: Optional[Any] = None,
    plugin_loader: Optional[Any] = None,
    tool_execution: Optional[Any] = None,
    tool_memo: Optional[Any] = None,
    tracer: Optional[Any] = None,
    progress: Optional[Any] = None,
) -> HarnessFacade:
    """创建 Harness 门面。"""
    return HarnessFacade(
        tool_registry=tool_registry,
        skill_loader=skill_loader,
        mcp_store=mcp_store,
        mcp_manager=mcp_manager,
        mcp_lifecycle=mcp_lifecycle,
        plugin_loader=plugin_loader,
        tool_execution=tool_execution,
        tool_memo=tool_memo,
        tracer=tracer,
        progress=progress,
    )


def build_harness(**kwargs: Any) -> HarnessFacade:
    """兼容旧调用方的门面工厂。"""
    return build_harness_facade(**kwargs)
