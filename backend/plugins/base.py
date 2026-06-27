"""Base classes every plugin subclasses + minimal runtime surface.

A plugin is a Python module on disk under
``<plugins_dir>/<plugin-id>/plugin.py`` with a sibling
``plugin.json`` manifest. Its single entry point is a class derived
from :class:`Plugin` exposing a ``setup(runtime)`` method; the runtime
is the loader's view of the host (a :class:`PluginRuntime`) and is
the only stable surface plugins should reach for.

v0.23 deliberately ships:

* Tool registration only (skill bundles, MemoryProvider implementations,
  hot-reload, capability sandboxing all deferred).
* No process isolation — plugins run in the host process and have
  every privilege the host does. The README and ``plugins_enabled=False``
  default make that crystal clear.
"""
from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:  # pragma: no cover — type-checkers only
    from pathlib import Path

    from ..memory.provider import MemoryProvider
    from ..tools.base import Tool
    from ..tools.registry import ToolRegistry


class PluginKind(str, Enum):
    """What surface a plugin contributes. Listed in ``manifest.kinds``.

    Unknown values in a manifest are silently dropped from the typed
    tuple but preserved in :attr:`PluginManifest.raw` for audit.
    """

    TOOLS = "tools"
    SKILLS = "skills"
    MEMORY = "memory"  # reserved for future MemoryProvider plugins


class PluginRuntime:
    """Tightly-scoped view of the host that the loader hands to plugins.

    The plugin author calls :meth:`register_tool` from their
    :meth:`Plugin.setup` to expose tools. ``workspace_dir`` is
    surfaced so plugins can persist files under their own subdirectory
    without us having to thread the path through every import.

    The runtime tracks names of tools the plugin registered so the
    loader can attribute them in :class:`LoadedPlugin.registered_tools`.
    """

    def __init__(
        self,
        *,
        plugin_id: str,
        tool_registry: Optional["ToolRegistry"],
        memory_manager: Optional[Any] = None,
        workspace_dir: Optional["Path"] = None,
        plugin_dir: Optional["Path"] = None,
    ) -> None:
        self.plugin_id = plugin_id
        self._tool_registry = tool_registry
        self._memory_manager = memory_manager
        self.workspace_dir = workspace_dir
        self.plugin_dir = plugin_dir
        self.registered_tools: list[str] = []
        self.registered_memory_providers: list[str] = []

    def register_tool(self, tool: "Tool") -> None:
        """Add ``tool`` to the shared :class:`ToolRegistry`.

        No-op if the loader was constructed without a registry (smoke
        tests do this to exercise manifest parsing without side effects).
        Duplicate names are surfaced as a :class:`ValueError` from the
        underlying registry, which the loader catches and turns into
        an ``error`` status.
        """
        if self._tool_registry is None:
            self.registered_tools.append(getattr(tool, "name", "<unknown>"))
            return
        self._tool_registry.register(tool)
        self.registered_tools.append(tool.name)

    def register_memory_provider(self, provider: "MemoryProvider") -> None:
        if self._memory_manager is None:
            self.registered_memory_providers.append(
                getattr(provider, "name", provider.__class__.__name__)
            )
            return
        self._memory_manager.add_provider(
            provider,
            plugin_id=self.plugin_id,
            plugin_dir=self.plugin_dir,
            workspace_dir=self.workspace_dir,
        )
        self.registered_memory_providers.append(
            getattr(provider, "name", provider.__class__.__name__)
        )


class Plugin:
    """Base class every plugin subclasses.

    Subclasses MUST set :attr:`id` and :attr:`version` to match the
    sibling ``plugin.json``. The loader compares them and refuses any
    mismatch (defence against accidentally importing the wrong module).

    Subclasses override :meth:`setup` to register tools / skill bundles
    against the supplied :class:`PluginRuntime`.
    """

    id: str = ""
    version: str = "0.0.0"

    def setup(self, runtime: PluginRuntime) -> None:  # pragma: no cover
        """Plug content into the runtime. Default is a no-op."""
        return None


__all__ = [
    "Plugin",
    "PluginKind",
    "PluginRuntime",
]
