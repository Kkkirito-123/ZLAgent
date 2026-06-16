"""Redis-backed temporary-data facade (sync variant).

Why sync, not async?
--------------------
ZLAgent's memory providers (`SessionContextProvider`, `WikiStore`,
the `_BUNDLE_CACHE` helpers) all expose **synchronous** methods.
``run_turn`` is an ``async`` coroutine that calls those providers
in-line; making the providers async would propagate the change
through ~30 call sites with no runtime benefit. Each Redis command
runs in 0.5-3 ms locally; blocking the event loop for that long is
unmeasurable next to the surrounding LLM calls (300-5000 ms each).

If we ever need to fan out hundreds of Redis ops per turn we can
revisit and migrate to ``redis.asyncio``; until then, the simpler
sync surface keeps the call sites clean and the smoke tests fast.

Why this module exists
----------------------
ZLAgent's three hottest "temporary data" surfaces — recent session
turns, the 60-second ``travel_realtime`` bundle cache and the
``wiki`` answer cache — were each kept in process-local state
(``deque``s, a module dict, SQLite). That design is fine for a
single worker but becomes incoherent the moment we run multiple
processes (gunicorn workers, horizontal scale-out, even a
quickly-restarted container that loses its warm RAM). The
user-facing symptom: a chatbot session losing "上海 → 杭州" the
moment the worker that heard the first turn is replaced.

Redis joins as a shared hot layer in front of the existing stores:

* **Primary (Redis)** — cross-process, TTL, sub-ms reads.
  Strongly consistent across workers; if Redis misses we fall
  through.
* **Secondary (current store)** — in-memory dict / SQLite / JSONL.
  Continues to hold the source of truth so Redis being down or
  evicted never loses user data.

Fault tolerance
---------------
Every method is best-effort:

* ``set`` / ``delete`` / ``push_trim`` return ``bool`` — ``False``
  on failure, never raise.
* ``get`` / ``lrange`` return ``None`` / ``[]`` on failure.
* Connection errors trip a short circuit-breaker (5 s by default)
  so we don't hammer a dead server on every turn.

Callers MUST check the return value and fall through to their
legacy store on ``None`` / ``False``. They MUST NOT treat Redis as
authoritative — see the layered design above.
"""
from __future__ import annotations

import json
import time
from typing import Any, Optional

from loguru import logger


class RedisBackend:
    """Thin wrapper around :mod:`redis`'s synchronous client.

    Construction is cheap and never performs I/O. The first real
    command lazily creates the client. Use :meth:`health_check`
    during startup to surface connectivity problems early; the rest
    of the API tolerates a missing / broken backend silently.
    """

    def __init__(
        self,
        url: str,
        *,
        key_prefix: str = "zlagent",
        connect_timeout_seconds: float = 2.0,
        command_timeout_seconds: float = 1.0,
        breaker_cooldown_seconds: float = 5.0,
        client: Any = None,
    ) -> None:
        self._url = (url or "").strip()
        self._key_prefix = key_prefix.strip().strip(":") or "zlagent"
        self._connect_timeout = max(0.1, float(connect_timeout_seconds))
        self._command_timeout = max(0.05, float(command_timeout_seconds))
        self._breaker_cooldown = max(0.0, float(breaker_cooldown_seconds))
        self._breaker_open_until = 0.0
        self._client = client
        self._client_is_external = client is not None

    # ------------------------------------------------------------------
    # Wiring
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """``True`` when the backend was configured (URL or injected client).

        Does not check connectivity — callers that want a hard health
        probe should call :meth:`health_check` at startup.
        """
        return bool(self._client_is_external or self._url)

    @property
    def key_prefix(self) -> str:
        return self._key_prefix

    def namespaced(self, *parts: str) -> str:
        """Return ``prefix:part1:part2`` for consistent keying across modules."""
        clean = [p for p in (self._key_prefix, *parts) if p]
        return ":".join(clean)

    def _get_client(self) -> Optional[Any]:
        """Lazy-create the redis-py client.

        Returns ``None`` when the backend is unconfigured or while
        the breaker is open. The import is deferred so pure-offline
        tests that never touch Redis do not pay the ``redis-py``
        import cost.
        """
        if not self.enabled:
            return None
        if self._breaker_open_until and time.monotonic() < self._breaker_open_until:
            return None
        if self._client is not None:
            return self._client
        try:
            import redis  # local import by design
        except Exception as exc:  # noqa: BLE001 - redis dep missing / broken
            logger.warning("[redis] import failed ({}); disabling backend", exc)
            self._open_breaker(self._breaker_cooldown * 4)
            return None
        try:
            self._client = redis.Redis.from_url(
                self._url,
                socket_connect_timeout=self._connect_timeout,
                socket_timeout=self._command_timeout,
                decode_responses=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[redis] from_url failed url={} err={}; disabling backend",
                self._redacted_url(), exc,
            )
            self._open_breaker(self._breaker_cooldown * 4)
            return None
        return self._client

    def health_check(self) -> bool:
        """Ping the server once and report. Logs but never raises.

        Callers are expected to treat a ``False`` return as "keep
        running with the in-memory/JSONL paths".
        """
        client = self._get_client()
        if client is None:
            return False
        try:
            pong = client.ping()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[redis] health_check failed url={} err={}; breaker open for {}s",
                self._redacted_url(), exc, self._breaker_cooldown,
            )
            self._note_failure()
            return False
        ok = bool(pong)
        if ok:
            logger.info(
                "[redis] connected url={} prefix={}",
                self._redacted_url(), self._key_prefix,
            )
        return ok

    def close(self) -> None:
        """Best-effort client close. Swallows errors."""
        client = self._client
        self._client = None
        if client is None or self._client_is_external:
            return
        try:
            client.close()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[redis] close failed: {}", exc)

    # ------------------------------------------------------------------
    # Keyed string commands
    # ------------------------------------------------------------------

    def get(self, key: str) -> Optional[str]:
        client = self._get_client()
        if client is None:
            return None
        try:
            value = client.get(key)
        except Exception as exc:  # noqa: BLE001
            self._note_failure()
            logger.debug("[redis] get({}) failed: {}", key, exc)
            return None
        if value is None:
            return None
        return value if isinstance(value, str) else value.decode("utf-8", errors="replace")

    def set(
        self,
        key: str,
        value: str,
        *,
        ttl_seconds: Optional[int] = None,
    ) -> bool:
        client = self._get_client()
        if client is None:
            return False
        try:
            if ttl_seconds and ttl_seconds > 0:
                client.set(key, value, ex=int(ttl_seconds))
            else:
                client.set(key, value)
            return True
        except Exception as exc:  # noqa: BLE001
            self._note_failure()
            logger.debug("[redis] set({}) failed: {}", key, exc)
            return False

    def delete(self, *keys: str) -> bool:
        if not keys:
            return True
        client = self._get_client()
        if client is None:
            return False
        try:
            client.delete(*keys)
            return True
        except Exception as exc:  # noqa: BLE001
            self._note_failure()
            logger.debug("[redis] delete({}) failed: {}", keys, exc)
            return False

    def expire(self, key: str, ttl_seconds: int) -> bool:
        if ttl_seconds <= 0:
            return False
        client = self._get_client()
        if client is None:
            return False
        try:
            client.expire(key, int(ttl_seconds))
            return True
        except Exception as exc:  # noqa: BLE001
            self._note_failure()
            logger.debug("[redis] expire({}) failed: {}", key, exc)
            return False

    def get_json(self, key: str) -> Optional[Any]:
        raw = self.get(key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (ValueError, TypeError) as exc:
            logger.debug("[redis] get_json({}) parse failed: {}", key, exc)
            return None

    def set_json(
        self,
        key: str,
        value: Any,
        *,
        ttl_seconds: Optional[int] = None,
    ) -> bool:
        try:
            payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            logger.debug("[redis] set_json({}) serialize failed: {}", key, exc)
            return False
        return self.set(key, payload, ttl_seconds=ttl_seconds)

    # ------------------------------------------------------------------
    # List / session commands
    # ------------------------------------------------------------------

    def push_trim(
        self,
        key: str,
        value: str,
        *,
        max_length: int,
        ttl_seconds: Optional[int] = None,
    ) -> bool:
        """Append to the tail, trim to ``max_length`` and refresh TTL.

        ``RPUSH + LTRIM + EXPIRE`` is the canonical "bounded log of
        recent events" idiom; we fold the three commands into one
        Python call so callers do not duplicate the boilerplate. The
        operations run via a redis-py pipeline so they reach the
        server in a single round-trip.
        """
        if max_length <= 0:
            return False
        client = self._get_client()
        if client is None:
            return False
        try:
            pipe = client.pipeline()
            pipe.rpush(key, value)
            pipe.ltrim(key, -max_length, -1)
            if ttl_seconds and ttl_seconds > 0:
                pipe.expire(key, int(ttl_seconds))
            pipe.execute()
            return True
        except Exception as exc:  # noqa: BLE001
            self._note_failure()
            logger.debug("[redis] push_trim({}) failed: {}", key, exc)
            return False

    def lrange(self, key: str, start: int = 0, end: int = -1) -> list[str]:
        client = self._get_client()
        if client is None:
            return []
        try:
            rows = client.lrange(key, start, end)
        except Exception as exc:  # noqa: BLE001
            self._note_failure()
            logger.debug("[redis] lrange({}) failed: {}", key, exc)
            return []
        out: list[str] = []
        for r in rows or []:
            if isinstance(r, str):
                out.append(r)
            else:
                try:
                    out.append(r.decode("utf-8", errors="replace"))
                except AttributeError:
                    out.append(str(r))
        return out

    def scan_delete(self, pattern: str, *, batch: int = 200) -> int:
        """Delete every key matching ``pattern``. Returns # deleted.

        SCAN avoids the O(N) block of ``KEYS``; we keep ``batch``
        small so we don't starve other commands on busy production
        instances. Best-effort: any failure stops early and returns
        whatever we did manage to delete.
        """
        client = self._get_client()
        if client is None:
            return 0
        deleted = 0
        try:
            cursor = 0
            while True:
                cursor, keys = client.scan(cursor=cursor, match=pattern, count=batch)
                if keys:
                    client.delete(*keys)
                    deleted += len(keys)
                if not cursor:
                    break
        except Exception as exc:  # noqa: BLE001
            self._note_failure()
            logger.debug(
                "[redis] scan_delete({}) partial: deleted={} err={}",
                pattern, deleted, exc,
            )
        return deleted

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _open_breaker(self, cooldown_seconds: float) -> None:
        until = time.monotonic() + max(0.0, cooldown_seconds)
        self._breaker_open_until = until

    def _note_failure(self) -> None:
        self._open_breaker(self._breaker_cooldown)

    def _redacted_url(self) -> str:
        """Redact any ``user:password@`` segment before logging."""
        url = self._url or "(injected)"
        try:
            if "@" in url:
                scheme_split = url.split("://", 1)
                if len(scheme_split) == 2:
                    scheme, rest = scheme_split
                    _, host_part = rest.split("@", 1)
                    return f"{scheme}://***@{host_part}"
        except Exception:  # noqa: BLE001
            pass
        return url


def build_redis_backend(
    url: str,
    *,
    key_prefix: str = "zlagent",
    connect_timeout_seconds: float = 2.0,
    command_timeout_seconds: float = 1.0,
) -> Optional[RedisBackend]:
    """Factory used by :mod:`backend.app`.

    Returns ``None`` when ``url`` is empty so callers can ``if backend:``
    without juggling sentinel instances. A non-empty URL always builds
    a backend — health-check is the caller's responsibility.
    """
    if not (url or "").strip():
        return None
    return RedisBackend(
        url,
        key_prefix=key_prefix,
        connect_timeout_seconds=connect_timeout_seconds,
        command_timeout_seconds=command_timeout_seconds,
    )


__all__ = ["RedisBackend", "build_redis_backend"]
