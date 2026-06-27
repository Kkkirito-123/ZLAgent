"""Skill usage telemetry sidecar — feeds the curator (v0.11+).

Mirrors Hermes' ``tools/skill_usage.py`` design choices:

* **Sidecar JSON, not frontmatter**. ``workspace/skills/.usage.json`` is
  the single source of truth for usage counts and lifecycle state. We
  deliberately keep this OUT of each skill's ``SKILL.md`` so:
    - human-edited SKILL.md content stays clean and review-friendly
    - agent-rewriting a skill's body never has to merge with telemetry
    - the file can be deleted to reset all state without losing skills
* **Atomic writes**. Every save goes through ``tempfile.mkstemp`` +
  ``os.replace``; a crash mid-write can't leave a half-written sidecar.
* **Best-effort mutators**. Every public ``record_*`` function catches
  all exceptions and logs at WARNING level — a broken sidecar must
  never break the underlying tool call (e.g. a ``read_file`` of a
  SKILL.md should still succeed even if the sidecar is corrupt).
* **`created_by="agent"` is the curator gate**. Skills that the agent
  authored during a background review fork are eligible for autonomous
  lifecycle management; everything else (user-authored, bundled in the
  docker image, manually copied in) is off-limits and never enters the
  sidecar. This is the *negative* definition Hermes uses; we follow it.

Lifecycle states::

    active    # default — recently used / new
    stale     # no view/use/patch for ``stale_after_days`` (curator may flag)
    archived  # moved out of active set; reversible via ``mark_active``
    pinned    # orthogonal flag; pinned skills bypass all auto-transitions

The curator (v0.11) reads this sidecar to make decisions; this module
itself does NOT make any auto-transitions.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from loguru import logger

# ---------------------------------------------------------------------------
# State sentinels
# ---------------------------------------------------------------------------

STATE_ACTIVE = "active"
STATE_STALE = "stale"
STATE_ARCHIVED = "archived"
VALID_STATES = frozenset({STATE_ACTIVE, STATE_STALE, STATE_ARCHIVED})

CREATED_BY_USER = "user"      # foreground turn (DM-driven)
CREATED_BY_AGENT = "agent"    # background_review fork (v0.9)
CREATED_BY_BUNDLED = "bundled"  # shipped with the docker image, never curated

USAGE_FILENAME = ".usage.json"
USAGE_EVENTS_FILENAME = ".usage_events.jsonl"  # v0.43 phase B+ — event log

# Event kinds in usage_events.jsonl. Mirror the ``record_*`` methods.
EVENT_VIEW = "view"      # SkillLoader.read_body
EVENT_USE = "use"        # explicit read_file of SKILL.md
EVENT_PATCH = "patch"    # skill_manage edit / patch / write_file
VALID_EVENT_KINDS = frozenset({EVENT_VIEW, EVENT_USE, EVENT_PATCH})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_iso(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _empty_record() -> Dict[str, Any]:
    """Default shape for one skill's sidecar entry."""
    return {
        "created_by": None,         # "user" | "agent" | "bundled"
        "use_count": 0,             # read_file -> SKILL.md reads
        "view_count": 0,            # SkillLoader.read_body() loads
        "patch_count": 0,           # skill_manage edit / patch / write_file mutations
        "last_used_at": None,
        "last_viewed_at": None,
        "last_patched_at": None,
        "created_at": _now_iso(),
        "state": STATE_ACTIVE,
        "pinned": False,
        "archived_at": None,
        "absorbed_into": None,      # set on dedup-archive: the survivor's name
    }


def latest_activity_at(record: Dict[str, Any]) -> Optional[str]:
    """Newest of last_used_at / last_viewed_at / last_patched_at.

    Excludes ``created_at`` so the curator can distinguish "never used"
    from "used long ago"; both are stale by lifecycle, but only the
    former is also a candidate for early archival.
    """
    best_dt: Optional[datetime] = None
    best_raw: Optional[str] = None
    for key in ("last_used_at", "last_viewed_at", "last_patched_at"):
        raw = record.get(key)
        dt = _parse_iso(raw)
        if dt is None:
            continue
        if best_dt is None or dt > best_dt:
            best_dt = dt
            best_raw = str(raw)
    return best_raw


def activity_count(record: Dict[str, Any]) -> int:
    total = 0
    for key in ("use_count", "view_count", "patch_count"):
        try:
            total += int(record.get(key) or 0)
        except (TypeError, ValueError):
            pass
    return total


def is_curator_managed(record: Optional[Dict[str, Any]]) -> bool:
    """True iff this record opts in to autonomous curator management.

    Strict invariant: only ``created_by="agent"`` records are curator-
    managed. Bundled / user-authored skills are off-limits regardless of
    how stale they look.
    """
    if not isinstance(record, dict):
        return False
    return record.get("created_by") == CREATED_BY_AGENT


# ---------------------------------------------------------------------------
# UsageStore — the I/O layer
# ---------------------------------------------------------------------------

class UsageStore:
    """File-backed sidecar store for skill usage telemetry.

    Constructed once per process with the workspace's ``skills/`` dir;
    the sidecar lives at ``<skills_dir>/.usage.json``. All operations
    are best-effort — see module docstring.

    Concurrency: this is a single-process tool, so we rely on the GIL
    and atomic ``os.replace`` rather than a real lock. If multiple
    workers ever start writing the sidecar concurrently the worst
    case is a lost update (one mutator's count being overwritten by
    another), which is benign for telemetry.
    """

    def __init__(self, skills_dir: Path) -> None:
        self._skills_dir = Path(skills_dir).resolve()

    # -- I/O -----------------------------------------------------------

    @property
    def usage_path(self) -> Path:
        return self._skills_dir / USAGE_FILENAME

    @property
    def usage_events_path(self) -> Path:
        """Append-only JSONL of per-bump events (v0.43 phase B+).

        The aggregate sidecar (``.usage.json``) is great for the
        curator but loses time-window detail — "how many times was X
        used in the past 24 h" is unanswerable from it alone. The
        events log fills that gap without changing any caller: each
        ``record_view / record_use / record_patch`` ALSO appends one
        row here so :class:`DailyReviewService` can compute true
        per-window counts.
        """
        return self._skills_dir / USAGE_EVENTS_FILENAME

    def _append_event(self, *, skill_name: str, kind: str) -> None:
        """Append one event row. Errors logged at WARNING, never raised."""
        if not skill_name or kind not in VALID_EVENT_KINDS:
            return
        try:
            self._skills_dir.mkdir(parents=True, exist_ok=True)
            row = {
                "ts": _now_iso(),
                "skill_name": str(skill_name),
                "kind": kind,
            }
            line = json.dumps(row, ensure_ascii=False) + "\n"
            # O_APPEND gives atomic line-granular writes on POSIX +
            # Windows alike (see skills/history.py for the same trick).
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
            if hasattr(os, "O_BINARY"):
                flags |= os.O_BINARY
            fd = os.open(self.usage_events_path, flags, 0o644)
            try:
                os.write(fd, line.encode("utf-8"))
            finally:
                os.close(fd)
        except Exception as exc:  # noqa: BLE001 — never break the caller
            logger.warning(
                "[skill_usage] event append failed ({} {}): {}",
                kind, skill_name, exc,
            )

    def events_since(
        self, after: datetime, *, limit: int = 10000,
    ) -> list[dict[str, Any]]:
        """Return events with ``ts >= after``, oldest-first.

        Reads the JSONL in reverse (cheaper for "past 24 h" queries
        because the file is append-only), stops as soon as one event
        falls outside the window, then reverses the collected slice
        to chronological order. Tolerant of garbage lines and a
        missing file — both return an empty list.
        """
        path = self.usage_events_path
        if not path.is_file():
            return []
        try:
            raw = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            logger.warning("[skill_usage] events read failed: {}", exc)
            return []
        if after.tzinfo is None:
            after = after.replace(tzinfo=timezone.utc)
        out: list[dict[str, Any]] = []
        for line in reversed(raw):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            ts = _parse_iso(obj.get("ts"))
            if ts is None:
                continue
            if ts < after:
                # Append-only file, sorted-by-time; older rows can't
                # be in window. Stop scanning.
                break
            out.append({
                "ts": obj.get("ts"),
                "skill_name": str(obj.get("skill_name") or ""),
                "kind": str(obj.get("kind") or ""),
            })
            if len(out) >= limit:
                break
        out.reverse()
        return out

    def load(self) -> Dict[str, Dict[str, Any]]:
        path = self.usage_path
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("[skill_usage] failed to read {}: {}", path, exc)
            return {}
        if not isinstance(data, dict):
            return {}
        clean: Dict[str, Dict[str, Any]] = {}
        for k, v in data.items():
            if isinstance(v, dict):
                clean[str(k)] = v
        return clean

    def _save(self, data: Dict[str, Dict[str, Any]]) -> None:
        path = self.usage_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(
                dir=str(path.parent), prefix=".usage_", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, sort_keys=True, ensure_ascii=False)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, path)
            except BaseException:
                # Clean up the temp file on any failure (incl. KeyboardInterrupt)
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception as exc:  # noqa: BLE001 - best-effort by design
            logger.warning("[skill_usage] failed to write {}: {}", path, exc)

    def get(self, skill_name: str) -> Dict[str, Any]:
        """Return the record for ``skill_name``, never raising.

        Backfills any missing keys from the empty-record template, so
        callers don't need to handle pre-v0.11 sidecar files.
        """
        if not skill_name:
            return _empty_record()
        rec = self.load().get(skill_name)
        if not isinstance(rec, dict):
            return _empty_record()
        merged = _empty_record()
        merged.update(rec)
        return merged

    def all(self) -> Dict[str, Dict[str, Any]]:
        """Return the entire sidecar map (with defaults backfilled)."""
        out: Dict[str, Dict[str, Any]] = {}
        for name, rec in self.load().items():
            merged = _empty_record()
            merged.update(rec)
            out[name] = merged
        return out

    # -- Mutation primitives ------------------------------------------

    def _mutate(self, skill_name: str, mutator) -> None:
        """Load → apply mutator → save. Never raises."""
        if not skill_name:
            return
        try:
            data = self.load()
            rec = data.get(skill_name)
            if not isinstance(rec, dict):
                rec = _empty_record()
            mutator(rec)
            data[skill_name] = rec
            self._save(data)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[skill_usage] mutate {} failed: {}", skill_name, exc)

    # -- Public mutators (called by skill_manage / read_file / loader) -

    def mark_created(
        self,
        skill_name: str,
        *,
        created_by: str,
        description: Optional[str] = None,
    ) -> None:
        """Record a brand-new skill. ``created_by`` is the provenance gate.

        We never overwrite an existing ``created_by`` — re-creating a
        skill that was previously authored by the user must NOT silently
        flip its provenance to ``agent``. The skill_manage layer is
        expected to refuse to overwrite without an explicit edit/patch.
        """
        def _set(rec: Dict[str, Any]) -> None:
            if not rec.get("created_by"):
                rec["created_by"] = created_by
            if not rec.get("created_at"):
                rec["created_at"] = _now_iso()
            if description and not rec.get("description"):
                rec["description"] = description
            rec["state"] = STATE_ACTIVE
        self._mutate(skill_name, _set)

    def record_view(self, skill_name: str) -> None:
        """Bump on ``SkillLoader.read_body()`` — the agent loaded the skill body."""
        def _bump(rec: Dict[str, Any]) -> None:
            rec["view_count"] = int(rec.get("view_count") or 0) + 1
            rec["last_viewed_at"] = _now_iso()
        self._mutate(skill_name, _bump)
        self._append_event(skill_name=skill_name, kind=EVENT_VIEW)

    def record_use(self, skill_name: str) -> None:
        """Bump on ``read_file`` of a SKILL.md — explicit user/tool read."""
        def _bump(rec: Dict[str, Any]) -> None:
            rec["use_count"] = int(rec.get("use_count") or 0) + 1
            rec["last_used_at"] = _now_iso()
        self._mutate(skill_name, _bump)
        self._append_event(skill_name=skill_name, kind=EVENT_USE)

    def record_patch(self, skill_name: str) -> None:
        """Bump on ``skill_manage`` edit / patch / write_file mutations."""
        def _bump(rec: Dict[str, Any]) -> None:
            rec["patch_count"] = int(rec.get("patch_count") or 0) + 1
            rec["last_patched_at"] = _now_iso()
        self._mutate(skill_name, _bump)
        self._append_event(skill_name=skill_name, kind=EVENT_PATCH)

    def set_pinned(self, skill_name: str, pinned: bool) -> None:
        def _set(rec: Dict[str, Any]) -> None:
            rec["pinned"] = bool(pinned)
        self._mutate(skill_name, _set)

    def set_state(self, skill_name: str, state: str) -> None:
        if state not in VALID_STATES:
            raise ValueError(
                f"invalid skill state {state!r}; must be one of {sorted(VALID_STATES)}"
            )

        def _set(rec: Dict[str, Any]) -> None:
            rec["state"] = state
            if state == STATE_ARCHIVED and not rec.get("archived_at"):
                rec["archived_at"] = _now_iso()
            elif state != STATE_ARCHIVED:
                rec["archived_at"] = None
        self._mutate(skill_name, _set)

    def archive(
        self, skill_name: str, *, absorbed_into: Optional[str] = None
    ) -> None:
        """Soft-archive. Reversible via :meth:`set_state` -> active.

        ``absorbed_into`` records the survivor of a dedupe merge so the
        decision is traceable (and recoverable) months later.
        """
        def _set(rec: Dict[str, Any]) -> None:
            rec["state"] = STATE_ARCHIVED
            rec["archived_at"] = _now_iso()
            if absorbed_into:
                rec["absorbed_into"] = absorbed_into
        self._mutate(skill_name, _set)

    def remove(self, skill_name: str) -> None:
        """Drop the record entirely (called when a skill dir is deleted).

        Note: actual file deletion is the caller's responsibility; this
        only cleans up the sidecar so a future skill of the same name
        starts with a fresh record.
        """
        if not skill_name:
            return
        try:
            data = self.load()
            if skill_name in data:
                del data[skill_name]
                self._save(data)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[skill_usage] remove {} failed: {}", skill_name, exc)
