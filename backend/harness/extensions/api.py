"""FastAPI surface for the harness extension inventory + lifecycle.

Routes mounted under ``/api/harness``:

* ``GET    /api/harness/extensions``                    — user-installed snapshot
* ``DELETE /api/harness/extensions/{kind}/{name}``      — uninstall (Phase 2)

The routes never return core tool / skill / YAML-MCP identifiers and
they refuse to remove anything classified as core — see
:mod:`backend.harness.core_manifest` for the classification policy.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Path, Request

from .. import Harness


router = APIRouter(prefix="/api/harness", tags=["harness"])


_VALID_KINDS = {"skill", "skills", "mcp", "mcps", "plugin", "plugins", "tool", "tools"}
_KIND_CANONICAL = {
    "skill": "skill", "skills": "skill",
    "mcp": "mcp", "mcps": "mcp",
    "plugin": "plugin", "plugins": "plugin",
    "tool": "tool", "tools": "tool",
}


def _get_harness(request: Request) -> Harness:
    harness = getattr(request.app.state, "harness", None)
    if harness is None:
        raise HTTPException(status_code=503, detail="harness not initialized")
    return harness


@router.get("/extensions")
async def list_extensions(request: Request) -> dict[str, Any]:
    """Return the user-installed skills / MCP / plugins / runtime tools.

    Core capabilities are intentionally absent from the lists. The
    ``core_summary`` block gives non-identifying counts so the user
    knows core capabilities are engaged without exposing names.
    """
    harness = _get_harness(request)
    inventory = harness.inventory()
    return inventory.to_dict()


@router.delete("/extensions/{kind}/{name}")
async def uninstall_extension(
    request: Request,
    kind: str = Path(..., description="One of skill(s), mcp(s), plugin(s), tool(s)"),
    name: str = Path(..., description="Extension identifier"),
) -> dict[str, Any]:
    """Uninstall a user extension. Refuses anything classified as core.

    Status mapping:
      * ``200`` — removal succeeded.
      * ``400`` — unknown ``kind``.
      * ``403`` — refused (target is core).
      * ``404`` — name not present in user inventory.
      * ``409`` — subsystem missing or unsupported (e.g. plugins).
      * ``500`` — backend error during delete.
    """
    if kind not in _VALID_KINDS:
        raise HTTPException(
            status_code=400,
            detail=f"invalid kind {kind!r}; expected one of {sorted(_VALID_KINDS)}",
        )
    canonical = _KIND_CANONICAL[kind]
    harness = _get_harness(request)

    if canonical == "skill":
        outcome = await harness.uninstall_skill(name)
    elif canonical == "mcp":
        outcome = await harness.uninstall_mcp(name)
    elif canonical == "plugin":
        outcome = await harness.uninstall_plugin(name)
    else:  # tool
        from .lifecycle import uninstall_tool
        outcome = await uninstall_tool(harness, name)

    body = outcome.to_dict()
    if outcome.ok:
        return body

    reason = outcome.refused_reason or ""
    if reason == "core":
        raise HTTPException(status_code=403, detail=body)
    if reason == "not_found":
        raise HTTPException(status_code=404, detail=body)
    if reason in ("missing_subsystem", "unsupported"):
        raise HTTPException(status_code=409, detail=body)
    raise HTTPException(status_code=500, detail=body)


__all__ = ["router"]
