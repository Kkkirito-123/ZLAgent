"""Core runtime overview helpers."""
from __future__ import annotations

from dataclasses import dataclass, field
from time import monotonic
from typing import Any, Optional


_OVERVIEW_CACHE_TTL_SECONDS = 1.5


@dataclass(slots=True)
class CoreRuntimeOverview:
    """Compact snapshot of the core runtime and extension surface."""

    core: dict[str, Any] = field(default_factory=dict)
    extensions: dict[str, Any] = field(default_factory=dict)
    services: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "core": dict(self.core),
            "extensions": dict(self.extensions),
            "services": dict(self.services),
        }


def _tool_registry_summary(tool_registry: Optional[Any]) -> dict[str, Any]:
    if tool_registry is None:
        return {"count": 0, "names": [], "counts_by_permission": {}}
    return {
        "count": len(tool_registry),
        "names": list(tool_registry.names()),
        "counts_by_permission": dict(tool_registry.counts_by_permission()),
    }


def _safe_count(value: Any) -> int:
    try:
        return len(value)
    except Exception:
        try:
            return sum(1 for _ in value)
        except Exception:
            return 0


def build_core_runtime_overview(*, app: Any) -> CoreRuntimeOverview:
    """Build a stable high-level snapshot for APIs and UI."""
    state = getattr(app, "state", None)
    tool_registry = getattr(state, "tool_registry", None)
    harness = getattr(state, "harness", None)
    gateway_manager = getattr(state, "gateway_manager", None)
    memory_manager = getattr(state, "memory_manager", None)
    skill_loader = getattr(state, "skill_loader", None)
    mcp_manager = getattr(state, "mcp_manager", None)
    plugin_loader = getattr(state, "plugin_loader", None)

    llm = getattr(state, "llm", None)
    core = {
        "tools": _tool_registry_summary(tool_registry),
        "llm_configured": bool(getattr(llm, "configured", False)),
        "progress_enabled": bool(harness and getattr(harness, "progress", None) is not None),
        "tracing_enabled": bool(harness and getattr(harness, "tracer", None) is not None),
        "memo_enabled": bool(harness and getattr(harness, "tool_memo", None) is not None),
    }

    started = monotonic()
    skills_items = []
    mcps_items = []
    plugins_items = []
    tools_items = []

    inv = None
    if harness is not None:
        try:
            inv = harness.inventory()
            skills_items = list(inv.skills)
            mcps_items = list(inv.mcps)
            plugins_items = list(inv.plugins)
            tools_items = list(inv.tools)
        except Exception:
            inv = None

    extensions = {
        "skills": len(skills_items) if skills_items else (_safe_count(skill_loader.list()) if skill_loader is not None else 0),
        "mcps": len(mcps_items) if mcps_items else (_safe_count(mcp_manager.status()) if mcp_manager is not None else 0),
        "plugins": len(plugins_items) if plugins_items else _safe_count(getattr(plugin_loader, "loaded", []) or []),
        "tools": len(tools_items) if tools_items else _safe_count(getattr(tool_registry, "list", lambda: [])()),
        "skills_items": skills_items,
        "mcps_items": mcps_items,
        "plugins_items": plugins_items,
        "tools_items": tools_items,
        "cache": {
            "generated_ms": int((monotonic() - started) * 1000),
            "source": "live",
        },
    }
    if inv is not None:
        try:
            extensions.update(inv.to_dict())
        except Exception:
            pass

    services = {
        "gateway_manager": gateway_manager is not None,
        "memory_manager": memory_manager is not None,
        "mcp_manager": mcp_manager is not None,
        "plugin_loader": plugin_loader is not None,
    }

    return CoreRuntimeOverview(core=core, extensions=extensions, services=services)


__all__ = ["CoreRuntimeOverview", "build_core_runtime_overview"]
