"""Core vs user-extension classification policy for the harness layer.

The harness presents a curated, *user-facing* view of the agent's
capabilities. Built-in tools, bundled skills, and YAML-declared MCP
servers are considered **core** and are hidden from this view:

* The user cannot list them via the harness inventory API.
* The user cannot remove them via the harness extension lifecycle.
* The agent still has full access to them internally.

Anything the user explicitly added at runtime is a **user extension**:

* MCP servers added via ``mcp_manage`` / ``POST /api/mcp/servers``.
* Skills authored via ``skill_manage`` and not on the curated core list.
* Plugins loaded from ``workspace/plugins``.

The policy is intentionally simple and conservative. Phase 1 errs on
the side of hiding rather than exposing: any unrecognized built-in
tool is treated as core. Future phases may relax this once the
inventory schema is stable.
"""
from __future__ import annotations

from typing import Iterable, Optional

# ---------------------------------------------------------------------------
# Core skill manifest
# ---------------------------------------------------------------------------

# Personal-AI general core skills. The harness inventory hides these from
# normal /list output even when they live under ``workspace/skills``.
#
# Curated from:
#   * ZLAgent's existing high-quality bundled skills (see plan v3).
#   * GitHub agent-skills research: Anthropic skills, Obra Superpowers,
#     addyosmani agent-skills, Block Goose marketplace.
#
# Phase 3 may add the new core skills (``using-skills``,
# ``verification-before-completion``, ``code-review-quality``,
# ``documentation-and-decisions``, ``document-processing``). They are
# listed here proactively so the harness UX is stable even before the
# SKILL.md files land.
CORE_SKILL_NAMES: frozenset[str] = frozenset({
    # Meta / extension management
    "mcp-discovery",
    "skill-authoring",
    "skill-vetter",
    "auto-updater",
    # Productivity / planning
    "task-planning",
    # Coding / debugging
    "systematic-debugging",
    # Research / digest
    "web-summary",
    "news-digest",
    "weather-now",
    "arxiv",
    "daily-paper-digest",
    # Travel / lifestyle
    "travel-guide",
    "travel-realtime-mcp",
    # Writing
    "humanizer",
    # New core skills (Phase 3 will materialize the SKILL.md files;
    # listing here lets the inventory contract be stable beforehand).
    "using-skills",
    "verification-before-completion",
    "code-review-quality",
    "documentation-and-decisions",
    "document-processing",
})

# Test-only / sample skills. Always hidden from the user inventory
# regardless of whether they appear under workspace/skills.
HIDDEN_SAMPLE_SKILL_NAMES: frozenset[str] = frozenset({
    "example-ping",
})


def is_core_skill(skill_id: str) -> bool:
    """Return True if ``skill_id`` is a curated core skill or hidden sample."""
    return skill_id in CORE_SKILL_NAMES or skill_id in HIDDEN_SAMPLE_SKILL_NAMES


# ---------------------------------------------------------------------------
# Core tool classification
# ---------------------------------------------------------------------------

# Tools whose name starts with this prefix come from runtime-attached
# MCP servers (see ``backend.mcp.tool_wrapper.make_qualified_tool_name``).
# These are always user extensions.
MCP_TOOL_PREFIX = "mcp__"


def is_user_tool_name(
    name: str,
    *,
    plugin_tool_names: Iterable[str] = (),
) -> bool:
    """Return True if ``name`` belongs to a user-installed extension.

    Two sources count as user extensions:

    * Runtime-attached MCP tools (``mcp__<server>__<tool>``).
    * Plugin-registered tools (names found in any
      :class:`backend.plugins.loader.LoadedPlugin.registered_tools`).

    Every other registered tool is treated as core and hidden from the
    user inventory.
    """
    if not name:
        return False
    if name.startswith(MCP_TOOL_PREFIX):
        return True
    if name in set(plugin_tool_names):
        return True
    return False


def is_core_tool_name(
    name: str,
    *,
    plugin_tool_names: Iterable[str] = (),
) -> bool:
    """Inverse of :func:`is_user_tool_name` — convenience wrapper."""
    return not is_user_tool_name(name, plugin_tool_names=plugin_tool_names)


# ---------------------------------------------------------------------------
# Core MCP classification
# ---------------------------------------------------------------------------

# Sources we treat as core (deployment-provided, not user-attached).
MCP_CORE_SOURCES: frozenset[str] = frozenset({"yaml"})

# Sources we treat as user-attached extensions.
MCP_USER_SOURCES: frozenset[str] = frozenset({"im", "rest"})


def is_user_mcp_source(source: Optional[str]) -> bool:
    """Return True for MCP rows the user added at runtime."""
    if not source:
        return False
    return source in MCP_USER_SOURCES


def is_core_mcp_source(source: Optional[str]) -> bool:
    """Return True for YAML-seeded MCP rows shipped with the deployment."""
    if not source:
        # Unknown source: be conservative and treat as core.
        return True
    return source in MCP_CORE_SOURCES


__all__ = [
    "CORE_SKILL_NAMES",
    "HIDDEN_SAMPLE_SKILL_NAMES",
    "MCP_CORE_SOURCES",
    "MCP_TOOL_PREFIX",
    "MCP_USER_SOURCES",
    "is_core_mcp_source",
    "is_core_skill",
    "is_core_tool_name",
    "is_user_mcp_source",
    "is_user_tool_name",
]
