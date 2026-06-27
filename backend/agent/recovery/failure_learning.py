"""Failure-pattern tracker — turns recurring tool errors into agent_notes.

Background
----------
After a few months of use, an agent will hit the same surprising error
over and over: the arxiv API returning 429 because we hit it too fast,
``read_url`` failing on a domain that requires a User-Agent header,
``cron_manage`` rejecting a name that has uppercase letters. Each
individual recovery is fine — the LLM apologises and retries — but the
n-th identical recovery is a sign the *agent* is the one needing to
remember.

This module is the lightweight bookkeeper that watches every turn for
``ok=False`` tool calls, fingerprints the failure (tool name + a stable
hash of the error string), increments a counter, and once the same
fingerprint crosses :data:`DEFAULT_FAILURE_THRESHOLD` it surfaces a
proactive proposal — currently a log line + a row in the
:class:`backend.memory.store.MemoryStore` so the v0.9 review fork (or
the operator) sees it next time.

Design choices

* **Best-effort, never raising.** The agent loop calls
  :meth:`FailureLearner.observe_turn` after every turn; that call wraps
  every operation in try/except so an exception in the bookkeeper never
  cascades into the user-facing turn.
* **Fingerprint-then-count.** We don't store every failure, just the
  *number* of times each fingerprint has fired. A fingerprint is
  ``(tool_name, normalised_error_signature)`` — see
  :func:`fingerprint_error`.
* **One pass per turn.** Even if a turn fires 5 retries of the same
  tool, this counts as 1 occurrence — we want "shows up across N turns",
  not "5 retries in one go".
* **Auto-write to memory only on the first crossing.** The threshold is
  inclusive: the **first** time a fingerprint hits N, we write
  exactly one ``agent_note``. Future occurrences of the same fingerprint
  are ignored from the auto-write path; the note already exists. The
  curator can dedupe / archive if needed.
* **v0.45 persistence.** Optionally pass a ``state_path``; the
  bookkeeper then loads its counters on startup and re-saves them
  best-effort on every threshold-relevant bump. Without a state_path
  the tracker stays in-memory exactly like v0.13.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from loguru import logger

from ...memory.scanner import scan_content
from ...memory.store import (
    KIND_AGENT_NOTE,
    SOURCE_REVIEW,
    MemoryError as _MemoryError,
    MemoryStore,
)


# Optional extension hook: a callback fired exactly when a fingerprint
# first crosses the failure threshold (i.e. the same moment we auto-
# write the agent_note). Originally introduced in v0.29 to feed the
# RuntimeSupervisor; that consumer is gone but the hook is left in
# place so future plug-ins can attach without touching FailureLearner.
# Implementations must never raise — the call is wrapped in try/except
# inside ``_observe`` for safety.
PatternFiredCallback = Callable[["FailureRecord"], None]


# When a fingerprint reaches this count, we auto-emit a memory note.
DEFAULT_FAILURE_THRESHOLD = 3

# Cap on the prefix of an error string we use for fingerprinting, so a
# 4 KB stack trace doesn't render every error unique.
_ERROR_FINGERPRINT_CHARS = 200

# Patterns we strip from the error before hashing — these are common
# random tokens that would otherwise make every occurrence look unique
# (request ids, timestamps, line numbers, addresses).
_NORMALISE_RES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\S*"), "<TS>"),
    (re.compile(r"0x[0-9a-fA-F]+"), "<HEX>"),
    (re.compile(r"\b[0-9a-fA-F]{8,}\b"), "<HEX>"),
    (re.compile(r"\bline \d+\b"), "line <N>"),
    (re.compile(r":\d+:\d+\b"), ":<L>:<C>"),
    (re.compile(r"\b\d+\b"), "<N>"),
]


def _parse_iso(value: Any) -> Optional[datetime]:
    """Best-effort ISO-8601 → datetime; returns None on anything weird."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def fingerprint_error(tool_name: str, error: Optional[str]) -> str:
    """Return a stable short fingerprint for ``(tool_name, error)``.

    Hash is 12 hex chars — long enough to avoid cross-tool collisions in
    practice (~17 trillion possible values) but short enough to read in
    logs without scrolling.
    """
    raw = (error or "").strip()[:_ERROR_FINGERPRINT_CHARS]
    for pattern, replacement in _NORMALISE_RES:
        raw = pattern.sub(replacement, raw)
    digest = hashlib.sha1(f"{tool_name}::{raw}".encode("utf-8")).hexdigest()[:12]
    return digest


@dataclass(slots=True)
class FailureRecord:
    """In-memory tally for one fingerprint."""

    tool_name: str
    error_preview: str
    count: int = 0
    first_seen_at: Optional[datetime] = None
    last_seen_at: Optional[datetime] = None
    note_written: bool = False  # set True after we auto-write a memory


class FailureLearner:
    """Per-process tally of recurring tool failures.

    Stateless w.r.t. persistence: failure history lives in process
    memory only. Restarting the process resets the counters — that is
    intentional. The agent_note we eventually write IS persistent (it
    lands in the user_memories table), so the lesson survives restarts.
    """

    def __init__(
        self,
        memory_store: MemoryStore,
        *,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        on_pattern_fired: Optional[PatternFiredCallback] = None,
        state_path: Optional[Path] = None,
    ) -> None:
        if failure_threshold < 2:
            raise ValueError("failure_threshold must be >= 2")
        self._store = memory_store
        self._threshold = failure_threshold
        self._records: dict[str, FailureRecord] = {}
        # Light lock — protects the counter dict across the unlikely
        # case of two turns observing in parallel from different IM
        # gateways. Cheap, single-process; no need for asyncio.Lock.
        self._lock = threading.Lock()
        # Optional fan-out hook for plug-ins that want to react to
        # fingerprint-threshold crossings. Default None preserves the
        # historical v0.13+ behaviour (auto-note only).
        self._on_pattern_fired = on_pattern_fired
        # v0.45 — optional persistence. When set, the bookkeeper loads
        # ``state_path`` on init (JSON: fingerprint -> record dict) and
        # rewrites it best-effort after every counter bump. Without a
        # state_path the tracker stays in-memory exactly like v0.13.
        self._state_path = state_path
        if state_path is not None:
            self._load_state()

    @property
    def records(self) -> dict[str, FailureRecord]:
        """Snapshot copy — caller can iterate without holding the lock."""
        with self._lock:
            return dict(self._records)

    def observe_turn(self, tool_outcomes: Iterable[tuple[str, bool, Optional[str]]]) -> None:
        """Bookkeep one turn's worth of tool outcomes.

        ``tool_outcomes`` is an iterable of ``(tool_name, ok, error)``.
        We deduplicate identical fingerprints WITHIN the turn — a five-
        retry storm on the same arxiv 429 counts as one observation.
        """
        try:
            now = datetime.now(timezone.utc)
            seen_in_turn: set[str] = set()
            for tool_name, ok, error in tool_outcomes:
                if ok:
                    continue
                fp = fingerprint_error(tool_name or "", error or "")
                if fp in seen_in_turn:
                    continue
                seen_in_turn.add(fp)
                self._bump(fp, tool_name, error, now)
        except Exception as exc:  # noqa: BLE001 - bookkeeper must never raise
            logger.warning("[failure_learning] observe_turn failed: {}", exc)

    # =================================================================
    # Internals
    # =================================================================

    def _bump(
        self,
        fp: str,
        tool_name: str,
        error: Optional[str],
        now: datetime,
    ) -> None:
        with self._lock:
            rec = self._records.get(fp)
            if rec is None:
                rec = FailureRecord(
                    tool_name=tool_name or "<unknown>",
                    error_preview=(error or "")[:200],
                    count=0,
                    first_seen_at=now,
                )
                self._records[fp] = rec
            rec.count += 1
            rec.last_seen_at = now
            should_write = (
                not rec.note_written and rec.count >= self._threshold
            )
            if should_write:
                rec.note_written = True
        logger.info(
            "[failure_learning] {} '{}' x{} (fp={})",
            "FIRED" if should_write else "tally",
            rec.tool_name, rec.count, fp,
        )
        if should_write:
            self._auto_write_note(rec)
            # Fire the extension hook on the same threshold crossing.
            # Wrapped in try/except so a buggy callback can never escape
            # into the agent loop.
            if self._on_pattern_fired is not None:
                try:
                    self._on_pattern_fired(rec)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "[failure_learning] on_pattern_fired callback raised: {}",
                        exc,
                    )
        # v0.45 — persist counters after every bump so a process restart
        # doesn't reset everyone's failure history to zero. Best-effort:
        # disk errors are logged but never raised into the agent.
        self._save_state()

    # =================================================================
    # Persistence (v0.45)
    # =================================================================

    def _load_state(self) -> None:
        """Restore counters from ``state_path`` if present.

        Schema: ``{fingerprint: {tool_name, error_preview, count,
        first_seen_at, last_seen_at, note_written}}``. All keys are
        plain JSON; datetimes are stored as ISO-8601 strings. Missing
        or malformed file is a clean slate, not a crash.
        """
        if self._state_path is None:
            return
        path = self._state_path
        try:
            if not path.exists():
                return
            raw = path.read_text(encoding="utf-8")
            payload = json.loads(raw or "{}")
            if not isinstance(payload, dict):
                logger.warning(
                    "[failure_learning] state file {} top-level is {}; ignoring",
                    path, type(payload).__name__,
                )
                return
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[failure_learning] state file {} unreadable: {}", path, exc,
            )
            return
        for fp, row in payload.items():
            if not isinstance(row, dict):
                continue
            try:
                first_seen = _parse_iso(row.get("first_seen_at"))
                last_seen = _parse_iso(row.get("last_seen_at"))
                self._records[str(fp)] = FailureRecord(
                    tool_name=str(row.get("tool_name") or "<unknown>"),
                    error_preview=str(row.get("error_preview") or "")[:200],
                    count=int(row.get("count") or 0),
                    first_seen_at=first_seen,
                    last_seen_at=last_seen,
                    note_written=bool(row.get("note_written")),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[failure_learning] dropping bad state row {}: {}",
                    fp, exc,
                )
        logger.info(
            "[failure_learning] restored {} fingerprint(s) from {}",
            len(self._records), path,
        )

    def _save_state(self) -> None:
        """Atomically rewrite ``state_path`` with the current records.

        Best-effort: a disk-full or permission error logs and returns;
        the in-memory counters are still correct.
        """
        if self._state_path is None:
            return
        path = self._state_path
        try:
            with self._lock:
                snapshot = {
                    fp: {
                        "tool_name": rec.tool_name,
                        "error_preview": rec.error_preview,
                        "count": rec.count,
                        "first_seen_at": (
                            rec.first_seen_at.isoformat()
                            if rec.first_seen_at else None
                        ),
                        "last_seen_at": (
                            rec.last_seen_at.isoformat()
                            if rec.last_seen_at else None
                        ),
                        "note_written": rec.note_written,
                    }
                    for fp, rec in self._records.items()
                }
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(snapshot, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(path)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[failure_learning] could not persist state to {}: {}",
                path, exc,
            )

    def _auto_write_note(self, rec: FailureRecord) -> None:
        """Emit a single agent_note describing the recurring failure."""
        content = self._compose_note(rec)
        # Defensive: run the same scanner the memory_manage tool uses.
        # If our own note string somehow trips a threat pattern, log
        # instead of writing — better a missing note than a refused one
        # cascading into a bookkeeping crash.
        veto = scan_content(content)
        if veto is not None:
            logger.warning(
                "[failure_learning] auto-note blocked by scanner ({}): {!r}",
                veto, content[:120],
            )
            return
        try:
            row = self._store.add(
                content,
                kind=KIND_AGENT_NOTE,
                source=SOURCE_REVIEW,
                pinned=False,
            )
            logger.info(
                "[failure_learning] wrote agent_note #{} for recurring {} failure",
                row["id"], rec.tool_name,
            )
        except _MemoryError as exc:
            logger.warning(
                "[failure_learning] auto-note refused by store: {}", exc,
            )

    @staticmethod
    def _compose_note(rec: FailureRecord) -> str:
        """Render a short, scanner-safe note about the pattern.

        Kept under 250 chars so it fits cleanly in the system prompt
        block alongside other memories.
        """
        return (
            f"[failure_pattern] tool={rec.tool_name} 出现 {rec.count} 次相同错误"
            f"。错误片段：{rec.error_preview[:120]}。建议：下次遇到时先检查"
            f"输入或调用频率，必要时加重试 / 兜底。"
        )
