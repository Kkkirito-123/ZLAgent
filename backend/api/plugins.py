"""``/api/plugins`` listing of loaded plugin status."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request


def build_router() -> APIRouter:
    router = APIRouter(prefix="/api/plugins", tags=["plugins"])

    @router.get("")
    async def list_plugins(request: Request) -> dict[str, Any]:
        loader = getattr(request.app.state, "plugin_loader", None)
        if loader is None:
            return {"enabled": False, "items": [], "count": 0}
        items = [p.to_dict() for p in loader.loaded]
        return {
            "enabled": True,
            "items": items,
            "count": len(items),
            "loaded_count": sum(1 for p in items if p["status"] == "loaded"),
            "error_count": sum(1 for p in items if p["status"] == "error"),
        }

    return router


__all__ = ["build_router"]
