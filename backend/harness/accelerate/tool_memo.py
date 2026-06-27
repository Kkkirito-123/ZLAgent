"""Per-session memoization for idempotent tools.

The agent often calls the same ``web_search`` / ``read_url`` /
``tool_search`` in a long multi-tool turn (or across two turns in the
same IM session). The underlying tools are pure-fetch, so the second
call wastes time and tokens. This module adds a small, bounded,
per-session cache that short-circuits repeated invocations.

Design:

* Only tools whose name is in :data:`DEFAULT_MEMOIZABLE_TOOLS` are
  cached. Mutating / confirm-tier / time-sensitive tools never enter
  the memo even if read-only — they have side effects worth running.
* Cache key combines ``(tool_name, session_id, args_hash)``. Without
  a session id the entry is treated as global.
* TTL defaults to 600 seconds; entries past TTL are evicted on read.
* Total cache size capped at :data:`DEFAULT_CAPACITY`; FIFO eviction.

The cache is attached to the live :class:`ToolRegistry` by
:func:`attach_tool_memo` which monkey-patches ``execute`` once at
boot. Smoke can call :func:`detach_tool_memo` to restore the original.
"""
from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:  # pragma: no cover
    from ...tools import ToolRegistry, ToolResult


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


DEFAULT_TTL_SECONDS = 600
DEFAULT_CAPACITY = 256

# The conservative whitelist: pure-fetch tools whose output is a
# function of args only (no side effects, no real-time freshness need
# stronger than 10 minutes). Adding a name here means "trust me, the
# second call within TTL would have returned the same thing".
DEFAULT_MEMOIZABLE_TOOLS: frozenset[str] = frozenset({
    "web_search",
    "read_url",
    "tool_search",
})


# ---------------------------------------------------------------------------
# Cache implementation
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Entry:
    payload: "ToolResult"
    expires_at: float


@dataclass(slots=True)
class MemoStats:
    """Lightweight counters exposed via the observability API."""

    hits: int = 0
    misses: int = 0
    evictions: int = 0
    skipped: int = 0  # tool not in the memoizable set

    def to_dict(self) -> dict[str, int]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "evictions": self.evictions,
            "skipped": self.skipped,
        }


class ToolMemo:
    """In-memory bounded FIFO cache for idempotent tool calls."""

    def __init__(
        self,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        capacity: int = DEFAULT_CAPACITY,
        memoizable: Optional[Iterable[str]] = None,
    ) -> None:
        self._ttl = max(1, int(ttl_seconds))
        self._capacity = max(1, int(capacity))
        self._memoizable: frozenset[str] = (
            frozenset(memoizable) if memoizable is not None else DEFAULT_MEMOIZABLE_TOOLS
        )
        self._entries: "OrderedDict[str, _Entry]" = OrderedDict()
        self.stats = MemoStats()

    @property
    def memoizable_tools(self) -> frozenset[str]:
        return self._memoizable

    def is_memoizable(self, tool_name: str) -> bool:
        return tool_name in self._memoizable

    def cache_key(
        self,
        tool_name: str,
        arguments: Optional[dict[str, Any]],
        *,
        session_id: Optional[str] = None,
    ) -> str:
        """Compute a stable cache key for ``(tool, args, session)``."""
        try:
            args_blob = json.dumps(
                arguments or {}, sort_keys=True, ensure_ascii=False, default=str,
            )
        except (TypeError, ValueError):
            args_blob = repr(arguments)
        digest = hashlib.sha1(args_blob.encode("utf-8")).hexdigest()[:16]
        return f"{tool_name}::{session_id or '_'}::{digest}"

    def get(self, key: str) -> Optional["ToolResult"]:
        entry = self._entries.get(key)
        if entry is None:
            self.stats.misses += 1
            return None
        if entry.expires_at < time.monotonic():
            self.stats.evictions += 1
            self._entries.pop(key, None)
            self.stats.misses += 1
            return None
        # Move to MRU end so FIFO eviction tracks recency rather than
        # insertion strictly.
        self._entries.move_to_end(key)
        self.stats.hits += 1
        return entry.payload

    def put(self, key: str, payload: "ToolResult") -> None:
        # Only memoize successful results — caching a transient error
        # would lock the agent into the failure for TTL_SECONDS.
        if not getattr(payload, "ok", False):
            return
        self._entries[key] = _Entry(
            payload=payload,
            expires_at=time.monotonic() + self._ttl,
        )
        self._entries.move_to_end(key)
        while len(self._entries) > self._capacity:
            self._entries.popitem(last=False)
            self.stats.evictions += 1

    def mark_skipped(self) -> None:
        self.stats.skipped += 1

    def clear(self) -> None:
        self._entries.clear()
        self.stats = MemoStats()


# ---------------------------------------------------------------------------
# ToolRegistry attachment (boot-time monkey-patch)
# ---------------------------------------------------------------------------


# Sentinel attribute the patch sets on the registry so detach can
# restore the original execute() and so a double-attach is detected.
_PATCHED_FLAG = "_harness_memo_attached"
_ORIGINAL_EXECUTE_ATTR = "_harness_memo_original_execute"


def attach_tool_memo(
    registry: "ToolRegistry",
    memo: ToolMemo,
    *,
    session_id_getter: Optional[Any] = None,
) -> None:
    """Wrap ``registry.execute`` with a memoization layer.

    Safe to call once at boot; re-calling logs a warning and is a
    no-op. ``session_id_getter`` is an optional zero-arg callable that
    returns the current session id (used as part of the cache key);
    when ``None`` the cache key uses a global namespace and entries
    are shared across sessions.
    """
    if getattr(registry, _PATCHED_FLAG, False):
        logger.warning("[harness.memo] registry already wrapped; ignoring re-attach")
        return

    original_execute = registry.execute

    async def memoized_execute(
        name: str,
        arguments: Optional[dict[str, Any]] = None,
        *,
        allow_confirm: bool = False,
    ):
        if not memo.is_memoizable(name):
            memo.mark_skipped()
            return await original_execute(name, arguments, allow_confirm=allow_confirm)
        session_id = None
        if callable(session_id_getter):
            try:
                session_id = session_id_getter()
            except Exception:  # noqa: BLE001 — never let the getter sink the call
                session_id = None
        key = memo.cache_key(name, arguments, session_id=session_id)
        hit = memo.get(key)
        if hit is not None:
            logger.debug("[harness.memo] HIT {} session={}", name, session_id or "_")
            return hit
        result = await original_execute(name, arguments, allow_confirm=allow_confirm)
        memo.put(key, result)
        return result

    setattr(registry, _ORIGINAL_EXECUTE_ATTR, original_execute)
    setattr(registry, _PATCHED_FLAG, True)
    registry.execute = memoized_execute  # type: ignore[method-assign]
    logger.info(
        "[harness.memo] attached: ttl={}s capacity={} tools={}",
        memo._ttl, memo._capacity, sorted(memo.memoizable_tools),
    )


def detach_tool_memo(registry: "ToolRegistry") -> bool:
    """Restore ``registry.execute`` to its pre-attach implementation."""
    if not getattr(registry, _PATCHED_FLAG, False):
        return False
    original = getattr(registry, _ORIGINAL_EXECUTE_ATTR, None)
    if original is None:
        return False
    registry.execute = original  # type: ignore[method-assign]
    try:
        delattr(registry, _ORIGINAL_EXECUTE_ATTR)
    except AttributeError:
        pass
    setattr(registry, _PATCHED_FLAG, False)
    return True


__all__ = [
    "DEFAULT_CAPACITY",
    "DEFAULT_MEMOIZABLE_TOOLS",
    "DEFAULT_TTL_SECONDS",
    "MemoStats",
    "ToolMemo",
    "attach_tool_memo",
    "detach_tool_memo",
]
