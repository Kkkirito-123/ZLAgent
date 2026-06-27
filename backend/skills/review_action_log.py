"""ReviewActionLog — append-only audit + rollback for the daily review.

The Phase B service runs the end-of-day review fork autonomously: the
LLM can call ``memory_manage`` and ``skill_manage`` and those writes
land immediately. That's the right autonomy/throughput tradeoff for a
single-user system, but the operator still needs an emergency brake:
"the review just decided to create skill X — undo it."

This module gives them that brake. After every successful run, the
:class:`DailyReviewService` appends one record describing what
changed:

* ``new_memory_ids`` — rows the review *added* to the memory store.
* ``archived_memory_ids`` — rows the review *archived* (typically via
  ``memory_manage(consolidate)`` merging dupes).
* ``skill_history_added`` — slice of ``skill_history.jsonl`` entries
  produced during the window; informational only because reversing an
  arbitrary edit needs the pre-edit content (operator does that via
  git or the existing skill REST).

Rollback for **memory** is automatic and idempotent: re-archive
``new_memory_ids`` and unarchive ``archived_memory_ids``. The log
is append-only, so rollback writes a *new* tombstone row pointing at
the original ``run_id``; readers reconstruct the live state by
scanning for tombstones.

File format: JSONL at ``<workspace>/review_actions.jsonl``. One row
per record. Two record shapes:

* ``{"kind": "run", "run_id": int, "started_at", ..., "rolled_back_at": null}``
* ``{"kind": "rollback", "run_id": int, "rolled_back_at": "..."}``

We pick JSONL (not SQL) so it stays operator-readable / git-friendly
alongside ``skill_history.jsonl``.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from loguru import logger

REVIEW_ACTIONS_FILENAME = "review_actions.jsonl"

# Record kinds.
KIND_RUN = "run"
KIND_ROLLBACK = "rollback"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ReviewActionLog:
    """Append-only audit log of daily-review actions + rollback markers."""

    def __init__(self, workspace_dir: Path) -> None:
        self._workspace = Path(workspace_dir).resolve()
        self._path = self._workspace / REVIEW_ACTIONS_FILENAME

    @property
    def path(self) -> Path:
        return self._path

    # ====================================================================
    # writes
    # ====================================================================

    def append_run(
        self,
        *,
        started_at: str,
        finished_at: str,
        review_ok: bool,
        skill_calls: int,
        memory_calls: int,
        final_text: str,
        new_memory_ids: list[int],
        archived_memory_ids: list[int],
        skill_history_added: list[dict[str, Any]],
    ) -> int:
        """Append one ``run`` row, returns its assigned ``run_id``.

        ``run_id`` is the 1-based ordinal of run rows in the file —
        derived by scanning existing records. Sequential per file,
        stable across restarts (because the file IS the source of
        truth).
        """
        run_id = self._next_run_id()
        # Truncate final_text — full version stays in last_summary
        # only. The log is meant to be skimmable.
        ft = (final_text or "").strip()
        if len(ft) > 400:
            ft = ft[:399] + "\u2026"
        row = {
            "kind": KIND_RUN,
            "run_id": run_id,
            "started_at": started_at,
            "finished_at": finished_at,
            "review_ok": bool(review_ok),
            "skill_calls": int(skill_calls),
            "memory_calls": int(memory_calls),
            "final_text": ft,
            "new_memory_ids": [int(i) for i in new_memory_ids],
            "archived_memory_ids": [int(i) for i in archived_memory_ids],
            "skill_history_added": list(skill_history_added),
        }
        self._append(row)
        return run_id

    def append_rollback(
        self, run_id: int, *, note: Optional[str] = None,
    ) -> None:
        """Mark ``run_id`` rolled back. Idempotent at the reader level."""
        row = {
            "kind": KIND_ROLLBACK,
            "run_id": int(run_id),
            "rolled_back_at": _now_iso(),
            "note": note,
        }
        self._append(row)

    # ====================================================================
    # reads
    # ====================================================================

    def list_runs(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Return run records newest-first, each annotated with rollback state."""
        runs, rolled = self._load()
        ordered = sorted(
            runs.values(),
            key=lambda r: int(r.get("run_id", 0)),
            reverse=True,
        )[: max(1, limit)]
        for r in ordered:
            r["rolled_back_at"] = rolled.get(int(r["run_id"]))
        return ordered

    def get_run(self, run_id: int) -> Optional[dict[str, Any]]:
        runs, rolled = self._load()
        row = runs.get(int(run_id))
        if row is None:
            return None
        row = dict(row)  # copy — caller may mutate
        row["rolled_back_at"] = rolled.get(int(run_id))
        return row

    def is_rolled_back(self, run_id: int) -> bool:
        _, rolled = self._load()
        return int(run_id) in rolled

    # ====================================================================
    # internals
    # ====================================================================

    def _append(self, row: dict[str, Any]) -> None:
        try:
            self._workspace.mkdir(parents=True, exist_ok=True)
            line = json.dumps(row, ensure_ascii=False) + "\n"
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
            if hasattr(os, "O_BINARY"):
                flags |= os.O_BINARY
            fd = os.open(self._path, flags, 0o644)
            try:
                os.write(fd, line.encode("utf-8"))
            finally:
                os.close(fd)
        except Exception as exc:  # noqa: BLE001 — auditing must not break the host
            logger.warning(
                "[review_log] append failed for kind={}: {}",
                row.get("kind"), exc,
            )

    def _load(self) -> tuple[dict[int, dict[str, Any]], dict[int, str]]:
        """Return ``(runs_by_id, rollback_ts_by_id)``."""
        if not self._path.is_file():
            return {}, {}
        try:
            raw = self._path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            logger.warning("[review_log] read failed: {}", exc)
            return {}, {}
        runs: dict[int, dict[str, Any]] = {}
        rolled: dict[int, str] = {}
        for line in raw:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            kind = obj.get("kind")
            try:
                rid = int(obj.get("run_id"))
            except (TypeError, ValueError):
                continue
            if kind == KIND_RUN:
                runs[rid] = obj
            elif kind == KIND_ROLLBACK:
                ts = obj.get("rolled_back_at") or _now_iso()
                rolled[rid] = str(ts)
        return runs, rolled

    def _next_run_id(self) -> int:
        runs, _ = self._load()
        if not runs:
            return 1
        return max(runs.keys()) + 1
