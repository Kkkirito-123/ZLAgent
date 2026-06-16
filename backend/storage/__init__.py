"""shared storage backends (Redis + future fan-out).

The ``memory``/``wiki``/``tools`` subsystems each own their primary
persistence path (JSONL deque, SQLite, module dict).  This package
holds the side-car fast/shared layers they can opt into — currently
just a Redis client wrapper with synchronous callers, asyncio runtime
and automatic fallback on any connectivity problem.
"""
from __future__ import annotations

from .redis_backend import RedisBackend, build_redis_backend

__all__ = ["RedisBackend", "build_redis_backend"]
