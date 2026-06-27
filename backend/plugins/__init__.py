"""ZLAgent plugin system (v0.23).

Local-directory plugin loader with a tightly-scoped runtime surface so
external code can ship tools / skill bundles without touching core
imports. Pip-installable plugin packages, MemoryProvider plugins,
hot-reload, and capability sandboxing are all deferred to later
versions; the v0.23 surface is deliberately minimal:

  - load plugins from ``workspace/plugins/<id>/plugin.py`` + ``plugin.yaml``
  - expose them via ``GET /api/plugins`` with load status / errors
  - register their tools into the shared ``ToolRegistry`` after the same
    ``SkillGuard`` description scan that MCP tools go through
  - never let a misbehaving plugin break boot
"""
from .base import Plugin, PluginKind, PluginRuntime
from .loader import LoadedPlugin, PluginLoader
from .manifest import ManifestError, PluginManifest

__all__ = [
    "Plugin",
    "PluginKind",
    "PluginRuntime",
    "PluginLoader",
    "LoadedPlugin",
    "PluginManifest",
    "ManifestError",
]
