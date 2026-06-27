"""Travel domain bootstrap — single entry point for ``app.py``.

Wire travel-specific tools and API routes in one call. The function
is the *only* place ``app.py`` needs to know about travel. Adding
or removing the domain is one line.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from fastapi import FastAPI
    from ...tools import ToolRegistry
    from ...wiki.store import WikiStore
    from ...wiki.geo_store import GeoStore
    from ...storage.redis_backend import RedisBackend


def register_travel_domain(
    *,
    app: "FastAPI",
    tool_registry: "ToolRegistry",
    wiki_store: Optional["WikiStore"] = None,
    geo_store: Optional["GeoStore"] = None,
    redis_backend: Optional["RedisBackend"] = None,
    bundle_ttl_seconds: int = 60,
    base_url: str = "http://localhost:8000",
) -> None:
    """Register travel tools, REST endpoints, and Redis bundle cache.

    ``wiki_store`` + ``geo_store`` are required for the visited-map
    pieces; pass ``None`` to skip those. The realtime tool always
    registers.
    """
    from .realtime import TravelRealtimeTool, set_bundle_cache_redis_backend
    tool_registry.register(TravelRealtimeTool())
    set_bundle_cache_redis_backend(redis_backend, ttl_seconds=bundle_ttl_seconds)

    if wiki_store is not None and geo_store is not None:
        from .visited_map_tool import VisitedMapTool
        tool_registry.register(
            VisitedMapTool(wiki_store, geo_store, base_url=base_url)
        )
        from .api import router as visited_map_router
        app.include_router(visited_map_router)
