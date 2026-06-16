"""Travel domain — pluggable knowledge pack.

Bundles travel-specific behaviour:

* ``realtime.py``      — TravelRealtimeTool (12306 + Open-Meteo)
* ``visited_map.py``   — VisitedMapBuilder (HTML/JSON renderer)
* ``visited_map_tool.py`` — VisitedMapTool (agent-facing wrapper)
* ``routing.py``       — keyword-triggered realtime hint routing
* ``api.py``           — /api/wiki/visited-map REST endpoints
* ``bootstrap.py``     — register everything in one call

Depends on generic ``tools/`` + ``wiki/`` (geo_store) but no other
domain. To disable travel, comment out ``register_travel_domain``
in ``backend/app.py``.
"""
from .bootstrap import register_travel_domain

__all__ = ["register_travel_domain"]
