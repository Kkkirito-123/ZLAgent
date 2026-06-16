"""Lazy probe for ``mcp.client.streamable_http.streamablehttp_client``.

The Anthropic ``mcp`` Python SDK gates streamable-HTTP behind an
optional dependency that's not always installed. Probing once at
import time and caching the answer keeps :class:`MCPManager.start`
free of repeated import overhead and guarantees a deterministic
"available?" answer per process.

Three exports:

* :func:`http_client_factory` returns the streamable-HTTP factory
  callable, or ``None`` if the dependency is missing. Cached.
* :func:`http_transport_available` returns the boolean version,
  cheaper for "should I show this in /api/mcp" checks.
* :func:`reset_probe_for_test` clears the cache so smoke tests can
  flip dependencies in and out without process restart.

The actual HTTP wire-up (connection lifecycle, OAuth flow, tools/
list_changed) ships in v0.24 only carries the config +
probe surface.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from loguru import logger


# Three-state cache: ``None`` means "not yet probed", ``False`` means
# "probed and unavailable", any callable means "probed and available".
_FACTORY_CACHE: Optional[Any] = None
_PROBED: bool = False


def _do_probe() -> Optional[Callable[..., Any]]:
    """Import the streamable-HTTP factory if the dependency is present."""
    try:
        from mcp.client.streamable_http import (  # type: ignore[import-not-found]
            streamablehttp_client,
        )
    except ImportError as exc:
        logger.info(
            "[mcp/http] streamable_http transport unavailable: {}",
            exc,
        )
        return None
    except Exception as exc:  # noqa: BLE001
        # Any other failure during import — treat as unavailable
        # rather than letting it bubble into MCPManager.start.
        logger.warning(
            "[mcp/http] streamable_http import raised: {}: {}",
            type(exc).__name__, exc,
        )
        return None
    return streamablehttp_client


def http_client_factory() -> Optional[Callable[..., Any]]:
    """Return the cached factory callable or ``None`` if unavailable.

    Subsequent calls return the same object, so callers can use
    ``factory_a is factory_b`` as a stable probe identity.
    """
    global _FACTORY_CACHE, _PROBED
    if not _PROBED:
        _FACTORY_CACHE = _do_probe()
        _PROBED = True
    return _FACTORY_CACHE


def http_transport_available() -> bool:
    """Boolean version of :func:`http_client_factory` for quick checks."""
    return http_client_factory() is not None


def reset_probe_for_test() -> None:
    """Clear the cached probe result. Tests only — never call from prod."""
    global _FACTORY_CACHE, _PROBED
    _FACTORY_CACHE = None
    _PROBED = False


__all__ = [
    "http_client_factory",
    "http_transport_available",
    "reset_probe_for_test",
]
