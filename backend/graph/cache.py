"""Sidecar JSON cache for LLM-extracted knowledge-graph fragments.

Layout: ``<workspace>/graph_cache/<kb_id>.json`` ::

    {
      "<record_key>": {
        "nodes":  [ {id, kind, label, attrs}, ... ],
        "edges":  [ {source, target, kind, attrs}, ... ],
        "generated_at": "2026-05-13T...Z",
        "model": "deepseek-v4-pro",
        "schema": "paper" | "memory"
      },
      ...
    }

``record_key = f"{record_id}|{sha1(content[:max_chars])}"`` so editing a
record's body invalidates its cache entry without deleting unrelated rows.

Reads fail-open: if the JSON is corrupt the cache is treated as empty
(the file is left in place so the operator can inspect it).  Writes are
atomic via tempfile + os.replace so a crash mid-write cannot leave a
half-written file.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from loguru import logger


def record_cache_key(record_id: str, content: str, *, max_chars: int = 4000) -> str:
    """Stable per-record cache key — invalidates on content change."""
    body = (content or "")[:max_chars]
    digest = hashlib.sha1(body.encode("utf-8", errors="ignore")).hexdigest()[:16]
    return f"{record_id}|{digest}"


@dataclass(slots=True)
class CachedExtraction:
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]
    schema: str
    generated_at: str
    model: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": self.nodes,
            "edges": self.edges,
            "schema": self.schema,
            "generated_at": self.generated_at,
            "model": self.model,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CachedExtraction":
        return cls(
            nodes=list(data.get("nodes") or []),
            edges=list(data.get("edges") or []),
            schema=str(data.get("schema") or ""),
            generated_at=str(data.get("generated_at") or ""),
            model=str(data.get("model") or ""),
        )


class LLMGraphCache:
    """Per-KB JSON sidecar cache, atomic writes, fail-open reads."""

    def __init__(self, root_dir: Path) -> None:
        self._root = Path(root_dir)
        self._locks: dict[str, Lock] = {}

    def _lock_for(self, kb_id: str) -> Lock:
        lock = self._locks.get(kb_id)
        if lock is None:
            lock = Lock()
            self._locks[kb_id] = lock
        return lock

    def _path(self, kb_id: str) -> Path:
        safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in (kb_id or "default"))
        return self._root / f"{safe or 'default'}.json"

    def _load_all(self, kb_id: str) -> dict[str, dict[str, Any]]:
        path = self._path(kb_id)
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("[graph_cache] failed to read {}: {}", path, exc)
            return {}
        if not isinstance(data, dict):
            return {}
        return data

    def get(self, kb_id: str, key: str) -> CachedExtraction | None:
        with self._lock_for(kb_id):
            data = self._load_all(kb_id)
        entry = data.get(key)
        if not isinstance(entry, dict):
            return None
        try:
            return CachedExtraction.from_dict(entry)
        except Exception:  # noqa: BLE001 — corrupt entry, drop it
            return None

    def all_for_kb(self, kb_id: str) -> dict[str, CachedExtraction]:
        with self._lock_for(kb_id):
            data = self._load_all(kb_id)
        out: dict[str, CachedExtraction] = {}
        for key, entry in data.items():
            if not isinstance(entry, dict):
                continue
            try:
                out[key] = CachedExtraction.from_dict(entry)
            except Exception:  # noqa: BLE001
                continue
        return out

    def put(self, kb_id: str, key: str, extraction: CachedExtraction) -> None:
        with self._lock_for(kb_id):
            data = self._load_all(kb_id)
            data[key] = extraction.to_dict()
            self._atomic_write(kb_id, data)

    def delete(self, kb_id: str, key: str) -> bool:
        with self._lock_for(kb_id):
            data = self._load_all(kb_id)
            if key not in data:
                return False
            del data[key]
            self._atomic_write(kb_id, data)
            return True

    def _atomic_write(self, kb_id: str, data: dict[str, Any]) -> None:
        path = self._path(kb_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @staticmethod
    def now_iso() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")
