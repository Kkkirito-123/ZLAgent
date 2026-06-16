"""Cross-session declarative memory subsystem (v0.12).

Mirrors Hermes Agent's ``agent/memory_manager.py`` + ``tools/memory_tool.py``
for the single-user case: a small, opinionated memory layer that injects a
frozen snapshot into the system prompt so the LLM can refer to facts
about its operator across sessions.

Public surface:

* :class:`backend.memory.store.MemoryStore` — SQLite-backed CRUD.
* :class:`backend.memory.manager.MemoryManager` — system-prompt assembly,
  prefetch, ``<memory-context>`` fence sanitisation.
* :func:`backend.memory.scanner.scan_content` — refuse-on-injection helper
  used before any string is persisted as memory.
"""
from .intent import MemoryIntentSignal, detect_memory_intent
from .manager import MemoryManager
from .provider import BaseMemoryProvider, MemoryProvider
from .scanner import scan_content
from .session_context import SessionContextProvider
from .store import MemoryStore

__all__ = [
    "BaseMemoryProvider",
    "MemoryIntentSignal",
    "MemoryManager",
    "MemoryProvider",
    "MemoryStore",
    "SessionContextProvider",
    "detect_memory_intent",
    "scan_content",
]
