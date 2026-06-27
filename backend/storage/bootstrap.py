"""Redis backend boot helper.

Extracted in v0.40 from :func:`backend.app.lifespan`. Wraps the
v0.37.8 best-effort Redis client construction + health-check so the
FastAPI startup orchestration reads top-down.

Returning ``None`` is the documented disabled state — every
consumer (session-context provider, travel_realtime bundle cache,
wiki cache) transparently falls back to its in-memory + JSONL/SQLite
path when handed ``None``.
"""
from __future__ import annotations

from typing import Any, Optional

from loguru import logger

from .redis_backend import RedisBackend, build_redis_backend


def build_redis_backend_with_healthcheck(settings: Any) -> Optional[RedisBackend]:
    """Build the shared :class:`RedisBackend` and run a one-shot ping.

    The ping is best-effort: if it raises or returns False we keep
    the backend object alive (so the per-call breaker can recover
    when Redis comes back) but log a warning so the operator
    notices the degraded state. When the Redis URL isn't configured
    at all we just return ``None``.
    """
    backend = build_redis_backend(
        settings.redis_url,
        key_prefix=settings.redis_key_prefix,
        connect_timeout_seconds=settings.redis_connect_timeout_seconds,
        command_timeout_seconds=settings.redis_command_timeout_seconds,
    )
    if backend is None:
        logger.info(
            "[redis] disabled (set ZLAGENT_REDIS_URL=redis://host:6379/0 to"
            " enable cross-process session context + bundle/wiki cache)"
        )
        return None

    try:
        ok = backend.health_check()
    except Exception as exc:  # noqa: BLE001 - never let redis crash startup
        logger.warning("[redis] health_check raised {}; disabling backend", exc)
        ok = False
    if not ok:
        logger.warning(
            "[redis] startup ping failed; backend left enabled but"
            " breaker is hot. Reads/writes will keep falling back to"
            " in-memory + JSONL/SQLite until the next successful ping."
        )
    return backend
