"""Uninstall lifecycle for user-installed extensions.

Phase 2 ships removal only — install workflows for skills/MCP go
through their existing tools (``skill_manage``, ``mcp_manage``) which
the agent already calls when the user asks for a new capability. This
module is the **single delete surface** the user can reach via REST
(``DELETE /api/harness/extensions/...``) or IM (``/remove <name>``).

Design rules:

* Refuse anything classified as **core** by
  :mod:`backend.harness.core_manifest` — built-in tools, curated
  skills, YAML-seeded MCP servers, hidden samples.
* Delegate to the existing managers
  (:class:`SkillManageTool` / :class:`MCPLifecycleService`) instead of
  duplicating their logic; this keeps history / usage / store rows
  consistent with the agent-driven path.
* Plugin removal is deferred (PluginLoader is one-shot). Phase 2
  returns a polite refusal message.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING

from loguru import logger

from ..core_manifest import (
    is_core_mcp_source,
    is_core_skill,
    is_user_tool_name,
)

if TYPE_CHECKING:  # pragma: no cover - import cycle avoidance
    from .. import Harness


# ---------------------------------------------------------------------------
# Outcome contract
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class UninstallOutcome:
    """Single shape returned by every ``uninstall_*`` helper.

    ``refused_reason`` is one of ``"core"``, ``"not_found"``,
    ``"missing_subsystem"``, ``"unsupported"``. ``None`` means the call
    succeeded (``ok=True``) or failed with an unstructured backend
    error captured in ``message``.
    """

    ok: bool
    kind: str
    name: str
    message: str = ""
    refused_reason: Optional[str] = None

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "kind": self.kind,
            "name": self.name,
            "message": self.message,
            "refused_reason": self.refused_reason,
        }


_CORE_REFUSAL_TEMPLATE = (
    "{name!r} is a core {kind} and cannot be removed via the harness."
    " Core capabilities are bundled with the deployment; edit the"
    " repository if you really need to drop one."
)


# ---------------------------------------------------------------------------
# Skill uninstall
# ---------------------------------------------------------------------------


async def uninstall_skill(harness: "Harness", name: str) -> UninstallOutcome:
    """Delete a user-authored skill directory.

    Refuses any name on the core / hidden-sample manifest. On success
    the live :class:`SkillLoader` is re-scanned so a subsequent
    inventory call no longer surfaces the skill.
    """
    name = (name or "").strip()
    if not name:
        return UninstallOutcome(
            ok=False, kind="skill", name=name,
            message="missing skill name", refused_reason="not_found",
        )
    if is_core_skill(name):
        return UninstallOutcome(
            ok=False, kind="skill", name=name,
            message=_CORE_REFUSAL_TEMPLATE.format(name=name, kind="skill"),
            refused_reason="core",
        )
    registry = harness.tool_registry
    skill_tool = registry.get("skill_manage") if registry is not None else None
    if skill_tool is None:
        return UninstallOutcome(
            ok=False, kind="skill", name=name,
            message="skill subsystem not initialized",
            refused_reason="missing_subsystem",
        )
    # Calling .execute() directly bypasses the registry's confirm gate;
    # the user typing /remove (or invoking DELETE) IS the explicit
    # confirmation, so no second yes/no is required.
    try:
        result = await skill_tool.execute({"action": "delete", "skill_name": name})
    except Exception as exc:  # noqa: BLE001 — surface to caller
        logger.exception("[harness] uninstall_skill {!r} crashed", name)
        return UninstallOutcome(
            ok=False, kind="skill", name=name,
            message=f"{type(exc).__name__}: {exc}",
        )
    if not result.ok:
        # SkillManageTool returns a friendly error string for "skill not
        # found" — surface verbatim so the user sees the same wording
        # they would have seen via skill_manage.
        return UninstallOutcome(
            ok=False, kind="skill", name=name,
            message=result.error or "delete failed",
            refused_reason="not_found"
            if "does not exist" in (result.error or "")
            else None,
        )
    # Refresh the loader so /api/harness/extensions reflects the change
    # without waiting for the next slow rescan.
    if harness.skill_loader is not None:
        try:
            harness.skill_loader.load()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[harness] skill loader refresh failed: {}", exc)
    return UninstallOutcome(
        ok=True, kind="skill", name=name,
        message=result.content or f"removed skill {name!r}",
    )


# ---------------------------------------------------------------------------
# MCP uninstall
# ---------------------------------------------------------------------------


async def uninstall_mcp(harness: "Harness", name: str) -> UninstallOutcome:
    """Detach a runtime-attached MCP server.

    Refuses YAML-seeded servers (the static deployment seed). Delegates
    to :meth:`MCPLifecycleService.detach` so registry/manager/store
    rollback stays atomic.
    """
    name = (name or "").strip()
    if not name:
        return UninstallOutcome(
            ok=False, kind="mcp", name=name,
            message="missing MCP server name", refused_reason="not_found",
        )
    store = harness.mcp_store
    lifecycle = harness.mcp_lifecycle
    if store is None or lifecycle is None:
        return UninstallOutcome(
            ok=False, kind="mcp", name=name,
            message="MCP subsystem disabled",
            refused_reason="missing_subsystem",
        )
    try:
        stored = store.get(name)
    except Exception as exc:  # noqa: BLE001
        logger.exception("[harness] mcp_store.get crashed for {!r}", name)
        return UninstallOutcome(
            ok=False, kind="mcp", name=name,
            message=f"{type(exc).__name__}: {exc}",
        )
    if stored is None:
        # No persisted row — but a YAML server might still be live in
        # the manager. Refuse defensively rather than silently dropping
        # a static seed entry.
        manager = harness.mcp_manager
        if manager is not None and manager.get_config(name) is not None:
            return UninstallOutcome(
                ok=False, kind="mcp", name=name,
                message=_CORE_REFUSAL_TEMPLATE.format(name=name, kind="MCP server"),
                refused_reason="core",
            )
        return UninstallOutcome(
            ok=False, kind="mcp", name=name,
            message=f"MCP server {name!r} not found",
            refused_reason="not_found",
        )
    if is_core_mcp_source(stored.source):
        return UninstallOutcome(
            ok=False, kind="mcp", name=name,
            message=_CORE_REFUSAL_TEMPLATE.format(name=name, kind="MCP server"),
            refused_reason="core",
        )
    try:
        outcome = await lifecycle.detach(name)
    except Exception as exc:  # noqa: BLE001
        logger.exception("[harness] mcp_lifecycle.detach crashed for {!r}", name)
        return UninstallOutcome(
            ok=False, kind="mcp", name=name,
            message=f"{type(exc).__name__}: {exc}",
        )
    if not outcome.ok:
        return UninstallOutcome(
            ok=False, kind="mcp", name=name,
            message=outcome.error or "detach failed",
        )
    detail = (
        f"unregistered={len(outcome.unregistered)},"
        f" manager_dropped={outcome.manager_dropped},"
        f" store_deleted={outcome.store_deleted}"
    )
    return UninstallOutcome(
        ok=True, kind="mcp", name=name,
        message=f"detached MCP server {name!r} ({detail})",
    )


# ---------------------------------------------------------------------------
# Plugin uninstall (deferred)
# ---------------------------------------------------------------------------


async def uninstall_plugin(harness: "Harness", name: str) -> UninstallOutcome:
    """Plugin removal is not supported at runtime in the current build."""
    return UninstallOutcome(
        ok=False, kind="plugin", name=(name or "").strip(),
        message=(
            "plugin removal requires editing workspace/plugins/ and"
            " restarting the process; runtime detach is not implemented yet."
        ),
        refused_reason="unsupported",
    )


# ---------------------------------------------------------------------------
# Tool uninstall (refusal for now)
# ---------------------------------------------------------------------------


async def uninstall_tool(harness: "Harness", name: str) -> UninstallOutcome:
    """Refuse direct tool removal — tools live inside skills/MCP/plugins.

    The user should remove the **owning** extension (the MCP server or
    plugin), not individual tool wrappers.
    """
    name = (name or "").strip()
    if not name:
        return UninstallOutcome(
            ok=False, kind="tool", name=name,
            message="missing tool name", refused_reason="not_found",
        )
    if not is_user_tool_name(name):
        return UninstallOutcome(
            ok=False, kind="tool", name=name,
            message=_CORE_REFUSAL_TEMPLATE.format(name=name, kind="tool"),
            refused_reason="core",
        )
    return UninstallOutcome(
        ok=False, kind="tool", name=name,
        message=(
            "tools are managed via their owning extension;"
            f" remove the MCP server / plugin that registered {name!r} instead."
        ),
        refused_reason="unsupported",
    )


__all__ = [
    "UninstallOutcome",
    "uninstall_mcp",
    "uninstall_plugin",
    "uninstall_skill",
    "uninstall_tool",
]
