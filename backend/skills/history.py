"""Skill mutation history (v0.15).

Every ``skill_manage`` write — create / edit / patch / write_file / remove_file
/ delete — appends one JSON line to ``workspace/skills/.history.jsonl``.

Why a separate sidecar (and not, say, columns on the existing UsageStore JSON):

* JSONL is append-only and crash-safe with a single ``write+flush`` per
  entry — no race with the usage store's whole-file rewrite.
* Operators can ``tail -f`` it during a real-machine session to watch the
  agent evolve in real time.
* git-friendly: a JSON object per line diffs cleanly.

Each entry carries enough context to reconstruct what changed:

* ``timestamp`` (ISO 8601 UTC)
* ``skill_name``
* ``action`` (one of CREATE / EDIT / PATCH / WRITE_FILE / REMOVE_FILE / DELETE)
* ``file_path`` (relative path inside the skill, defaults to "SKILL.md")
* ``actor`` (``"user"`` / ``"agent"`` / ``"harness"`` from
  :mod:`backend.core.provenance`)
* ``before_hash`` / ``after_hash`` (sha1[:12] of the file contents; ``null``
  when the file did not exist before / no longer exists after)
* ``diff`` (unified-diff snippet capped at 4 KiB; ``null`` for binary or
  ``DELETE``)

Crashes during write are swallowed and logged at WARNING — the underlying
``skill_manage`` action succeeds even if the history sidecar is broken.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from loguru import logger


HISTORY_FILENAME = ".history.jsonl"
MAX_DIFF_BYTES = 4 * 1024  # 4 KiB
HASH_PREFIX_LEN = 12


# Action constants — frozen as module-level so callers can ``from .history
# import ACTION_PATCH`` and avoid magic strings.
ACTION_CREATE = "create"
ACTION_EDIT = "edit"
ACTION_PATCH = "patch"
ACTION_WRITE_FILE = "write_file"
ACTION_REMOVE_FILE = "remove_file"
ACTION_DELETE = "delete"


@dataclass(slots=True)
class HistoryEntry:
    timestamp: str
    skill_name: str
    action: str
    file_path: str
    actor: str
    before_hash: Optional[str]
    after_hash: Optional[str]
    diff: Optional[str]


class SkillHistoryStore:
    """Append-only JSONL log of skill mutations."""

    def __init__(self, skills_root: Path):
        self._root = Path(skills_root).resolve()
        self._path = self._root / HISTORY_FILENAME

    @property
    def path(self) -> Path:
        return self._path

    # -- recording ----------------------------------------------------------

    def record(
        self,
        *,
        skill_name: str,
        action: str,
        file_path: str = "SKILL.md",
        actor: str = "user",
        before_text: Optional[str] = None,
        after_text: Optional[str] = None,
    ) -> None:
        """Append one entry. Errors logged at WARNING, never raised."""
        try:
            entry = HistoryEntry(
                timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                skill_name=str(skill_name),
                action=str(action),
                file_path=str(file_path or "SKILL.md"),
                actor=str(actor or "user"),
                before_hash=_hash(before_text),
                after_hash=_hash(after_text),
                diff=_compute_diff(before_text, after_text, action=action),
            )
            self._append(entry)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[skill_history] failed to record {} on {}: {}",
                action, skill_name, exc,
            )

    # -- reading ------------------------------------------------------------

    def list_entries(
        self,
        *,
        skill_name: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict]:
        """Return entries newest-first, optionally filtered by skill_name."""
        if not self._path.is_file():
            return []
        try:
            raw = self._path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            logger.warning("[skill_history] could not read {}: {}", self._path, exc)
            return []
        out: list[dict] = []
        for line in reversed(raw):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if skill_name and obj.get("skill_name") != skill_name:
                continue
            out.append(obj)
            if len(out) >= limit:
                break
        return out

    def stats(self) -> dict:
        """Aggregate counters for the REST surface."""
        try:
            entries = self.list_entries(limit=100000)
        except Exception:  # noqa: BLE001 — defensive
            entries = []
        per_action: dict[str, int] = {}
        per_skill: dict[str, int] = {}
        for entry in entries:
            per_action[entry.get("action", "?")] = per_action.get(entry.get("action", "?"), 0) + 1
            per_skill[entry.get("skill_name", "?")] = per_skill.get(entry.get("skill_name", "?"), 0) + 1
        return {
            "total": len(entries),
            "per_action": per_action,
            "per_skill": per_skill,
            "path": str(self._path),
        }

    # -- internal -----------------------------------------------------------

    def _append(self, entry: HistoryEntry) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        line = json.dumps(asdict(entry), ensure_ascii=False)
        # Use os.O_APPEND for atomic concurrent writes — multiple workers
        # appending JSONL doesn't interleave at line granularity on POSIX.
        # On Windows the same is true with O_APPEND + text mode + small
        # writes (which ours always are).
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        fd = os.open(self._path, flags, 0o644)
        try:
            os.write(fd, (line + "\n").encode("utf-8"))
        finally:
            os.close(fd)


# -- pure helpers --------------------------------------------------------------

def _hash(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    h = hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()
    return h[:HASH_PREFIX_LEN]


def _compute_diff(before: Optional[str], after: Optional[str], *, action: str) -> Optional[str]:
    """Compute a unified-diff snippet, capped at MAX_DIFF_BYTES."""
    # DELETE has no after-state and any diff would just be "everything is
    # gone" which adds little value. The action + skill_name already says
    # the deletion happened.
    if action in (ACTION_DELETE,):
        return None
    if before is None and after is None:
        return None
    before_lines = (before or "").splitlines(keepends=True)
    after_lines = (after or "").splitlines(keepends=True)
    diff_iter = difflib.unified_diff(
        before_lines,
        after_lines,
        fromfile="before",
        tofile="after",
        n=2,  # 2 context lines — enough to locate the change without bloat
    )
    body = "".join(diff_iter)
    if not body:
        return ""
    encoded = body.encode("utf-8", errors="replace")
    if len(encoded) > MAX_DIFF_BYTES:
        truncated = encoded[: MAX_DIFF_BYTES - 64].decode("utf-8", errors="replace")
        body = truncated + "\n... [diff truncated]"
    return body


def actor_for_origin(origin) -> str:  # type: ignore[no-untyped-def]
    """Map a :mod:`provenance` origin enum/value to a history actor string."""
    try:
        from ..core.provenance import BACKGROUND_REVIEW, FOREGROUND
    except Exception:  # pragma: no cover — defensive
        return "user"
    if origin is BACKGROUND_REVIEW or origin == "background_review":
        return "agent"
    if origin is FOREGROUND or origin == "foreground":
        return "user"
    return str(origin or "user")
