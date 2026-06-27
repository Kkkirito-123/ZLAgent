"""Compute the user-facing extension inventory.

Reads the live registries/stores ZLAgent already maintains and filters
out anything classified as **core** by :mod:`backend.harness.core_manifest`.
The output is the snapshot served by ``/api/harness/extensions`` and
(in Phase 2) by the ``/list`` IM command.

No new persistence is introduced here — inventory is derived state.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from ..core_manifest import (
    CORE_SKILL_NAMES,
    HIDDEN_SAMPLE_SKILL_NAMES,
    is_core_skill,
    is_user_mcp_source,
    is_user_tool_name,
)


@dataclass(slots=True)
class HarnessInventory:
    """Snapshot returned by :func:`build_inventory`.

    The inventory now exposes two clear zones:

    * ``core`` — hidden capabilities that power the product but should
      not be user-installable or uninstallable from the public surface.
    * ``extensions`` — user-installed skills, MCP servers, plugins,
      and tools, including third-party tools like a paper-writing pack.

    This mirrors the "inside core / outside tools" mental model the
    user described and makes it easy for a UI to render the two regions
    separately.
    """

    skills: list[dict[str, Any]] = field(default_factory=list)
    mcps: list[dict[str, Any]] = field(default_factory=list)
    plugins: list[dict[str, Any]] = field(default_factory=list)
    tools: list[dict[str, Any]] = field(default_factory=list)
    core_summary: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        extensions = {
            "skills": list(self.skills),
            "mcps": list(self.mcps),
            "plugins": list(self.plugins),
            "tools": list(self.tools),
            "counts": {
                "skills": len(self.skills),
                "mcps": len(self.mcps),
                "plugins": len(self.plugins),
                "tools": len(self.tools),
                "all": len(self.skills) + len(self.mcps) + len(self.plugins) + len(self.tools),
            },
        }
        return {
            "core": dict(self.core_summary),
            "extensions": extensions,
            # Backward-compatible flat fields for existing callers.
            "skills": list(self.skills),
            "mcps": list(self.mcps),
            "plugins": list(self.plugins),
            "tools": list(self.tools),
            "core_summary": dict(self.core_summary),
            "counts": extensions["counts"],
        }


def _collect_plugin_tool_names(plugin_loader: Optional[Any]) -> set[str]:
    """Pull tool names plugins registered into the shared registry."""
    if plugin_loader is None:
        return set()
    names: set[str] = set()
    loaded = getattr(plugin_loader, "loaded", None) or []
    for plugin in loaded:
        try:
            registered = getattr(plugin, "registered_tools", None) or []
        except Exception:  # noqa: BLE001 — defensive against weird plugins
            registered = []
        for name in registered:
            if isinstance(name, str) and name:
                names.add(name)
    return names


def _skills_section(
    skill_loader: Optional[Any],
) -> tuple[list[dict[str, Any]], int]:
    """Return ``(user_skills, hidden_core_count)``.

    A user skill is one whose id is **not** on the core manifest and
    not in the hidden sample list. Hidden samples (``example-ping``)
    are counted but never surfaced.
    """
    if skill_loader is None:
        return [], 0
    try:
        manifests = list(skill_loader.list())
    except Exception:  # noqa: BLE001 — never break the inventory API
        return [], 0

    user: list[dict[str, Any]] = []
    hidden = 0
    for m in manifests:
        sid = getattr(m, "id", None) or getattr(m, "name", None) or ""
        if not isinstance(sid, str) or not sid:
            continue
        if is_core_skill(sid):
            hidden += 1
            continue
        user.append({
            "id": sid,
            "name": str(getattr(m, "name", sid) or sid),
            "description": str(getattr(m, "description", "") or ""),
            "tags": list(getattr(m, "tags", []) or []),
            "version": str(getattr(m, "version", "") or ""),
            "created_by": str(getattr(m, "created_by", "") or ""),
        })
    user.sort(key=lambda row: row["id"])
    return user, hidden


def _mcp_section(
    mcp_store: Optional[Any],
    mcp_manager: Optional[Any],
) -> tuple[list[dict[str, Any]], int]:
    """Return ``(user_mcps, hidden_core_count)``.

    User MCP rows come from the SQLite store and carry
    ``source in {im, rest}``. The live manager is consulted (when
    available) to add the ``connected`` flag for each row.
    """
    if mcp_store is None:
        return [], 0
    try:
        stored = list(mcp_store.list_all())
    except Exception:  # noqa: BLE001
        return [], 0

    status_by_name: dict[str, Any] = {}
    if mcp_manager is not None:
        try:
            for status in mcp_manager.status():
                status_by_name[status.name] = status
        except Exception:  # noqa: BLE001
            status_by_name = {}

    user: list[dict[str, Any]] = []
    hidden = 0
    for row in stored:
        cfg = getattr(row, "config", None)
        name = getattr(cfg, "name", None) if cfg is not None else None
        source = getattr(row, "source", None)
        if not isinstance(name, str) or not name:
            continue
        if not is_user_mcp_source(source):
            hidden += 1
            continue
        live = status_by_name.get(name)
        user.append({
            "name": name,
            "transport": str(getattr(cfg, "transport", "") or ""),
            "enabled": bool(getattr(cfg, "enabled", False)),
            "description": str(getattr(cfg, "description", "") or ""),
            "source": str(source or ""),
            "created_by": getattr(row, "created_by", None) or "",
            "connected": bool(getattr(live, "connected", False)) if live is not None else None,
            "tool_count": int(getattr(live, "tool_count", 0)) if live is not None else 0,
        })
    user.sort(key=lambda row: row["name"])
    return user, hidden


def _plugin_section(
    plugin_loader: Optional[Any],
) -> tuple[list[dict[str, Any]], int]:
    """Return ``(user_plugins, hidden_core_count)``.

    All locally-loaded plugins are user extensions; there is no core
    plugin concept yet. The hidden count is therefore always zero.
    """
    if plugin_loader is None:
        return [], 0
    loaded = getattr(plugin_loader, "loaded", None) or []
    rows: list[dict[str, Any]] = []
    for plugin in loaded:
        try:
            data = plugin.to_dict()
        except Exception:  # noqa: BLE001
            continue
        rows.append({
            "id": str(data.get("id") or ""),
            "version": str(data.get("version") or ""),
            "status": str(data.get("status") or ""),
            "error": data.get("error"),
            "description": str(data.get("description") or ""),
            "registered_tools": list(data.get("registered_tools") or []),
            "registered_memory_providers": list(
                data.get("registered_memory_providers") or []
            ),
        })
    rows.sort(key=lambda row: row["id"])
    return rows, 0


def _tools_section(
    tool_registry: Optional[Any],
    plugin_tool_names: set[str],
) -> tuple[list[dict[str, Any]], int]:
    """Return ``(user_tools, hidden_core_count)``.

    User tools = MCP-qualified tools + plugin-registered tools. Everything
    else (built-ins like ``read_file`` / ``web_search`` / ``skill_manage``)
    is treated as core and hidden from this surface.
    """
    if tool_registry is None:
        return [], 0
    try:
        tools = list(tool_registry.list())
    except Exception:  # noqa: BLE001
        return [], 0

    user: list[dict[str, Any]] = []
    hidden = 0
    for tool in tools:
        name = getattr(tool, "name", None)
        if not isinstance(name, str) or not name:
            continue
        if not is_user_tool_name(name, plugin_tool_names=plugin_tool_names):
            hidden += 1
            continue
        permission = getattr(tool, "permission", None)
        permission_value = getattr(permission, "value", str(permission or ""))
        user.append({
            "name": name,
            "permission": permission_value,
            "description": str(getattr(tool, "description", "") or ""),
            "is_read_only": bool(getattr(tool, "is_read_only", False)),
            "is_destructive": bool(getattr(tool, "is_destructive", False)),
            "origin": "mcp" if name.startswith("mcp__") else "plugin",
        })
    user.sort(key=lambda row: row["name"])
    return user, hidden


def build_inventory(
    *,
    tool_registry: Optional[Any] = None,
    skill_loader: Optional[Any] = None,
    mcp_store: Optional[Any] = None,
    mcp_manager: Optional[Any] = None,
    plugin_loader: Optional[Any] = None,
) -> HarnessInventory:
    """Assemble a :class:`HarnessInventory` from the live subsystems."""
    plugin_tool_names = _collect_plugin_tool_names(plugin_loader)
    skills, core_skills = _skills_section(skill_loader)
    mcps, core_mcps = _mcp_section(mcp_store, mcp_manager)
    plugins, core_plugins = _plugin_section(plugin_loader)
    tools, core_tools = _tools_section(tool_registry, plugin_tool_names)
    summary = {
        "skills_hidden": core_skills,
        "mcps_hidden": core_mcps,
        "plugins_hidden": core_plugins,
        "tools_hidden": core_tools,
        # Inform the user that core is engaged without leaking names.
        "core_skill_catalog_size": len(CORE_SKILL_NAMES),
        "hidden_sample_skill_count": len(HIDDEN_SAMPLE_SKILL_NAMES),
    }
    return HarnessInventory(
        skills=skills,
        mcps=mcps,
        plugins=plugins,
        tools=tools,
        core_summary=summary,
    )


__all__ = ["HarnessInventory", "build_inventory"]
