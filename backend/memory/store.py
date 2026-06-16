"""SQLAlchemy-backed CRUD for :class:`backend.db.models.UserMemory`.

This is the single source of truth for cross-session declarative memory.
The store is deliberately thin — no business rules, no LRU enforcement,
no system-prompt assembly. Those live in :class:`MemoryManager`, which
*uses* the store. Keeping the layers separate means we can rewrite the
manager (e.g. swap in a vector backend later) without churning the
storage code.

Capacity policy:

* Hard ceiling: at most :data:`DEFAULT_MAX_ENTRIES` non-archived entries.
  When ``add()`` would exceed this, we run an LRU sweep that targets
  agent-written, non-pinned, low-recall entries first; explicit and
  pinned entries are sacred.
* Char limit per entry: :data:`DEFAULT_MAX_ENTRY_CHARS` (~500). Anything
  longer is refused with a clear error rather than silently truncated;
  the caller (the LLM via ``memory_manage``) should split or summarise.

All mutations go through ``session_scope`` so a crash mid-write rolls
back cleanly. Reads return plain dicts so callers can pickle / serialise
without dragging ORM proxies out of session.
"""
from __future__ import annotations

from datetime import datetime
from difflib import SequenceMatcher
from typing import Any, Iterable, Optional

from loguru import logger
from sqlalchemy import and_, or_

from ..db.models import UserMemory
from ..db.session import session_scope

# -----------------------------------------------------------------------------
# Constants — exposed so the test harness and the tool description can use them
# -----------------------------------------------------------------------------

# Three semantic layers (v0.43, 钱学森三层递阶记忆体):
#
# * ``control_axiom`` (L1) — control-theory base logic seeded by
#   :func:`backend.memory.bootstrap.seed_control_axioms`. The first-
#   layer principles the agent thinks WITH (goal / state / deviation /
#   feedback / execution / correction / constraints). Bootstrapped
#   once per fresh DB; the ``memory_manage`` tool refuses to write
#   into this kind so the LLM can't quietly mutate its own thinking
#   doctrine. Operators can still curate via the REST surface.
# * ``user_fact`` (L3, scenario-grounded) — facts about the operator
#   and their world. Hermes' USER.md equivalent.
# * ``agent_note`` (L2, abstracted underlying logic) — the agent's
#   own working notes: trigger conditions, judgment criteria,
#   failure signals, environment quirks. Hermes' MEMORY.md equivalent
#   but with a tighter remit (one-off chat details should NOT land
#   here; only reusable underlying logic).
KIND_CONTROL_AXIOM = "control_axiom"
KIND_USER_FACT = "user_fact"
KIND_AGENT_NOTE = "agent_note"
VALID_KINDS = frozenset({KIND_CONTROL_AXIOM, KIND_USER_FACT, KIND_AGENT_NOTE})

# Where an entry came from. Drives curator-style trimming policy.
SOURCE_EXPLICIT = "explicit"   # user told the agent to remember
SOURCE_REVIEW = "review"       # the v0.9 background review fork wrote it
SOURCE_IMPORT = "import"       # REST API / seed data
VALID_SOURCES = frozenset({SOURCE_EXPLICIT, SOURCE_REVIEW, SOURCE_IMPORT})

DEFAULT_MAX_ENTRIES = 200
DEFAULT_MAX_ENTRY_CHARS = 500


# -----------------------------------------------------------------------------
# Public types
# -----------------------------------------------------------------------------


def _row_to_dict(row: UserMemory) -> dict[str, Any]:
    """ORM → plain dict (caller-safe, no session attachment)."""
    return {
        "id": row.id,
        "kind": row.kind,
        "content": row.content,
        "source": row.source,
        "pinned": bool(row.pinned),
        "archived": bool(row.archived),
        "recall_count": int(row.recall_count or 0),
        "last_recalled_at": row.last_recalled_at.isoformat() if row.last_recalled_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


# -----------------------------------------------------------------------------
# MemoryStore
# -----------------------------------------------------------------------------


class MemoryError(Exception):
    """Raised by :class:`MemoryStore` for caller-correctable problems
    (validation, capacity, invalid kind/source). The ``memory_manage``
    tool catches this and surfaces the message to the LLM verbatim so
    it can adapt (rephrase, split, forget something else first)."""


class MemoryStore:
    """SQLite CRUD layer for :class:`UserMemory` rows.

    Stateless: every method opens its own ``session_scope``. Safe to
    instantiate once per process and share across coroutines.
    """

    def __init__(
        self,
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        max_entry_chars: int = DEFAULT_MAX_ENTRY_CHARS,
    ) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be >= 1")
        if max_entry_chars < 16:
            raise ValueError("max_entry_chars must be >= 16")
        self._max_entries = max_entries
        self._max_entry_chars = max_entry_chars

    # =================================================================
    # Read
    # =================================================================

    def get(self, memory_id: int) -> Optional[dict[str, Any]]:
        with session_scope() as session:
            row = session.get(UserMemory, memory_id)
            return _row_to_dict(row) if row is not None else None

    def get_knowledge_bases(self) -> list[str]:
        with session_scope() as session:
            rows = (
                session.query(UserMemory.knowledge_base_id)
                .filter(UserMemory.archived.is_(False))
                .distinct()
                .all()
            )
            return [str(row[0]) for row in rows if row and row[0]]

    def list(
        self,
        *,
        kind: Optional[str] = None,
        include_archived: bool = False,
        limit: Optional[int] = None,
        knowledge_base_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Return rows in stable order (pinned first, then newest)."""
        with session_scope() as session:
            q = session.query(UserMemory)
            if not include_archived:
                q = q.filter(UserMemory.archived.is_(False))
            if knowledge_base_id is not None:
                q = q.filter(UserMemory.knowledge_base_id == knowledge_base_id)
            if kind is not None:
                if kind not in VALID_KINDS:
                    raise MemoryError(
                        f"invalid kind {kind!r}; expected one of {sorted(VALID_KINDS)}"
                    )
                q = q.filter(UserMemory.kind == kind)
            # Pinned first → newest first → stable id tiebreak.
            q = q.order_by(
                UserMemory.pinned.desc(),
                UserMemory.created_at.desc(),
                UserMemory.id.desc(),
            )
            if limit is not None:
                q = q.limit(limit)
            return [_row_to_dict(row) for row in q.all()]

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        include_archived: bool = False,
        knowledge_base_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Naive substring match on ``content``.

        Good enough for v0.12 single-user use; if entries grow past a few
        hundred we'll plug a vector index in here without changing the
        signature. Empty query returns the most-recent entries (handy for
        the manager's prefetch fallback).
        """
        q_str = (query or "").strip()
        with session_scope() as session:
            q = session.query(UserMemory)
            if not include_archived:
                q = q.filter(UserMemory.archived.is_(False))
            if knowledge_base_id is not None:
                q = q.filter(UserMemory.knowledge_base_id == knowledge_base_id)
            if q_str:
                # Match each whitespace-split token independently and OR
                # them — this lets a multi-word query catch entries that
                # would miss on full-string contains. Hermes' production
                # match is fancier but this is plenty for our scale.
                terms = [t for t in q_str.split() if len(t) >= 2]
                if terms:
                    q = q.filter(
                        or_(*[UserMemory.content.ilike(f"%{t}%") for t in terms])
                    )
            q = q.order_by(
                UserMemory.pinned.desc(),
                UserMemory.recall_count.desc(),
                UserMemory.created_at.desc(),
            ).limit(max(1, limit))
            return [_row_to_dict(row) for row in q.all()]

    def stats(self, *, knowledge_base_id: Optional[str] = None) -> dict[str, Any]:
        """Quick metrics for the REST and smoke tests."""
        with session_scope() as session:
            total_q = session.query(UserMemory)
            active_q = session.query(UserMemory).filter(UserMemory.archived.is_(False))
            pinned_q = session.query(UserMemory).filter(
                UserMemory.pinned.is_(True),
                UserMemory.archived.is_(False),
            )
            by_kind_q = {
                k: session.query(UserMemory)
                .filter(UserMemory.archived.is_(False), UserMemory.kind == k)
                for k in VALID_KINDS
            }
            if knowledge_base_id is not None:
                total_q = total_q.filter(UserMemory.knowledge_base_id == knowledge_base_id)
                active_q = active_q.filter(UserMemory.knowledge_base_id == knowledge_base_id)
                pinned_q = pinned_q.filter(UserMemory.knowledge_base_id == knowledge_base_id)
                by_kind_q = {
                    k: q.filter(UserMemory.knowledge_base_id == knowledge_base_id)
                    for k, q in by_kind_q.items()
                }
        return {
            "total": total_q.count(),
            "active": active_q.count(),
            "pinned": pinned_q.count(),
            "by_kind": {k: q.count() for k, q in by_kind_q.items()},
            "max_entries": self._max_entries,
            "max_entry_chars": self._max_entry_chars,
        }

    def compact(self) -> int:
        """Archive old non-pinned agent notes first when over capacity."""
        removed = 0
        with session_scope() as session:
            active_count = session.query(UserMemory).filter(
                UserMemory.archived.is_(False)
            ).count()
            if active_count <= self._max_entries:
                return 0
            over = active_count - self._max_entries
            rows = (
                session.query(UserMemory)
                .filter(
                    UserMemory.archived.is_(False),
                    UserMemory.pinned.is_(False),
                    UserMemory.kind != KIND_CONTROL_AXIOM,
                )
                .order_by(UserMemory.recall_count.asc(), UserMemory.created_at.asc())
                .limit(over)
                .all()
            )
            for row in rows:
                row.archived = True
                removed += 1
        if removed:
            logger.info("[memory] compacted {} row(s)", removed)
        return removed

    # =================================================================
    # Write
    # =================================================================

    def add(
        self,
        content: str,
        *,
        kind: str = KIND_USER_FACT,
        source: str = SOURCE_EXPLICIT,
        pinned: bool = False,
        knowledge_base_id: str = "default",
    ) -> dict[str, Any]:
        """Insert a new row, enforcing kind/source/length/capacity rules."""
        cleaned = (content or "").strip()
        if not cleaned:
            raise MemoryError("memory content must be non-empty")
        if len(cleaned) > self._max_entry_chars:
            raise MemoryError(
                f"memory content {len(cleaned)} chars exceeds limit"
                f" {self._max_entry_chars}; split or summarise"
            )
        if kind not in VALID_KINDS:
            raise MemoryError(
                f"invalid kind {kind!r}; expected one of {sorted(VALID_KINDS)}"
            )
        if source not in VALID_SOURCES:
            raise MemoryError(
                f"invalid source {source!r}; expected one of {sorted(VALID_SOURCES)}"
            )

        with session_scope() as session:
            # Drop exact duplicates inside the same kind silently — they
            # add nothing and would just clutter the prompt budget.
            existing = (
                session.query(UserMemory)
                .filter(
                    UserMemory.kind == kind,
                    UserMemory.knowledge_base_id == knowledge_base_id,
                    UserMemory.archived.is_(False),
                    UserMemory.content == cleaned,
                )
                .first()
            )
            if existing is not None:
                # Treat as a "touch": bump recall_count + return without
                # creating a new row. Keeps the canonical entry stable.
                existing.recall_count = int(existing.recall_count or 0) + 1
                existing.last_recalled_at = datetime.utcnow()
                session.flush()
                return _row_to_dict(existing)

            self._maybe_evict(session)

            row = UserMemory(
                knowledge_base_id=knowledge_base_id,
                kind=kind,
                content=cleaned,
                source=source,
                pinned=pinned,
            )
            session.add(row)
            session.flush()
            session.refresh(row)
            return _row_to_dict(row)

    def remove(self, memory_id: int, *, allow_pinned: bool = False) -> bool:
        """Hard-delete a row. Returns True iff it existed.

        Pinned entries are refused unless ``allow_pinned`` is set — the
        review fork is never given that flag, the operator REST endpoint
        is.
        """
        with session_scope() as session:
            row = session.get(UserMemory, memory_id)
            if row is None:
                return False
            if row.pinned and not allow_pinned:
                raise MemoryError(
                    f"memory #{memory_id} is pinned; refuse to remove."
                    " Use the operator REST endpoint with force=true,"
                    " or unpin first."
                )
            session.delete(row)
            return True

    def archive(self, memory_id: int) -> bool:
        with session_scope() as session:
            row = session.get(UserMemory, memory_id)
            if row is None:
                return False
            row.archived = True
            row.updated_at = datetime.utcnow()
            return True

    def unarchive(self, memory_id: int) -> bool:
        with session_scope() as session:
            row = session.get(UserMemory, memory_id)
            if row is None:
                return False
            row.archived = False
            row.updated_at = datetime.utcnow()
            return True

    def set_pinned(self, memory_id: int, pinned: bool) -> bool:
        with session_scope() as session:
            row = session.get(UserMemory, memory_id)
            if row is None:
                return False
            row.pinned = bool(pinned)
            row.updated_at = datetime.utcnow()
            return True

    def touch_recall(self, memory_ids: Iterable[int]) -> None:
        """Bump recall_count + last_recalled_at for entries the manager
        actually surfaced this turn. Best-effort; never raises."""
        ids = [int(i) for i in memory_ids if i is not None]
        if not ids:
            return
        try:
            with session_scope() as session:
                rows = session.query(UserMemory).filter(UserMemory.id.in_(ids)).all()
                now = datetime.utcnow()
                for row in rows:
                    row.recall_count = int(row.recall_count or 0) + 1
                    row.last_recalled_at = now
        except Exception as exc:  # noqa: BLE001 - telemetry must not raise
            logger.warning("[memory] touch_recall failed: {}", exc)

    # =================================================================
    # Internal — capacity management
    # =================================================================

    def _maybe_evict(self, session) -> None:  # noqa: ANN001 - sa.orm.Session
        """LRU eviction when about to exceed the active-row ceiling.

        Strategy: archive (NOT delete) the oldest, lowest-recall,
        agent-written, non-pinned entries until we're under the cap.
        Explicit-source rows and pinned rows are skipped. Truly
        irreducible overflow (everything pinned/explicit) raises so the
        LLM sees a clear ``MemoryError`` and can ask the user for
        guidance.
        """
        active = session.query(UserMemory).filter(
            UserMemory.archived.is_(False),
        ).count()
        if active < self._max_entries:
            return

        evictable = (
            session.query(UserMemory)
            .filter(
                UserMemory.archived.is_(False),
                UserMemory.pinned.is_(False),
                UserMemory.source != SOURCE_EXPLICIT,
            )
            .order_by(
                UserMemory.recall_count.asc(),
                UserMemory.last_recalled_at.asc().nulls_first(),
                UserMemory.created_at.asc(),
            )
            .limit(active - self._max_entries + 1)
            .all()
        )
        if not evictable:
            raise MemoryError(
                f"memory at capacity ({active}/{self._max_entries}) and"
                " every active entry is either pinned or explicitly"
                " written by the user. Forget or unpin one before adding."
            )
        now = datetime.utcnow()
        for row in evictable:
            row.archived = True
            row.updated_at = now
            logger.info(
                "[memory] evicted #{} (kind={} source={} recall={}) — capacity",
                row.id, row.kind, row.source, row.recall_count,
            )

    # =================================================================
    # Consolidation (v0.15)
    # =================================================================

    # Rank used for tie-breaking when two near-duplicate entries are
    # merged. Higher is "more authoritative" — explicit entries beat
    # review-fork entries beat REST imports.
    _SOURCE_PRIORITY: dict[str, int] = {
        SOURCE_EXPLICIT: 3,
        SOURCE_IMPORT: 2,
        SOURCE_REVIEW: 1,
    }

    def consolidate(
        self,
        *,
        similarity_threshold: float = 0.80,
        kind: Optional[str] = None,
        max_pairs: int = 50,
    ) -> dict[str, Any]:
        """Find and merge near-duplicate active memories within each bucket.

        Merge policy (deterministic, no LLM):

        1. Compute pairwise :class:`SequenceMatcher` ratio inside each
           ``kind`` bucket, skipping pinned entries entirely (pinned is
           sacred, never auto-merged).
        2. When a pair scores ≥ ``similarity_threshold``:

           * Survivor wins on **pin > source priority > recall_count >
             newer ``created_at``** (left-to-right precedence).
           * Loser is **archived** (not deleted) so the operator can
             restore via REST if the heuristic was wrong.
           * Survivor inherits the loser's ``recall_count`` and bumps
             ``last_recalled_at`` so it doesn't drop in priority.
        3. Hard refusal: an ``explicit`` row is never archived to make
           way for a ``review`` row. The opposite direction is allowed
           because the explicit version is the authoritative restate.

        Returns a structured report (no exceptions on empty / nothing
        merged) so the caller can echo the decisions back to the LLM
        or operator.
        """
        if not 0.0 < similarity_threshold <= 1.0:
            raise MemoryError(
                "similarity_threshold must be in (0.0, 1.0]"
            )
        if kind is not None and kind not in VALID_KINDS:
            raise MemoryError(
                f"invalid kind {kind!r}; expected one of {sorted(VALID_KINDS)}"
            )
        kinds = (kind,) if kind else tuple(sorted(VALID_KINDS))

        merges: list[dict[str, Any]] = []
        considered_pairs = 0
        with session_scope() as session:
            for k in kinds:
                rows = (
                    session.query(UserMemory)
                    .filter(
                        UserMemory.archived.is_(False),
                        UserMemory.pinned.is_(False),
                        UserMemory.kind == k,
                    )
                    .order_by(UserMemory.created_at.asc())
                    .all()
                )
                if len(rows) < 2:
                    continue
                # We mutate rows[] as we archive losers — track ids
                # already merged-out so we don't pair them again.
                archived_ids: set[int] = set()
                for i, left in enumerate(rows):
                    if left.id in archived_ids:
                        continue
                    for right in rows[i + 1:]:
                        if right.id in archived_ids:
                            continue
                        considered_pairs += 1
                        if considered_pairs > max_pairs:
                            break
                        ratio = SequenceMatcher(
                            None, left.content or "", right.content or ""
                        ).ratio()
                        if ratio < similarity_threshold:
                            continue
                        # Pick survivor / loser deterministically.
                        survivor, loser = self._rank_merge_candidates(left, right)
                        if survivor is None or loser is None:
                            continue
                        # Refuse to archive explicit-source rows in
                        # favour of weaker provenance — "the user said
                        # this themselves" outranks the review fork's
                        # autonomous addition every time.
                        if (
                            loser.source == SOURCE_EXPLICIT
                            and survivor.source != SOURCE_EXPLICIT
                            and not survivor.pinned
                        ):
                            continue
                        # Archive the loser, fold counters into survivor.
                        loser.archived = True
                        loser.updated_at = datetime.utcnow()
                        survivor.recall_count = (
                            int(survivor.recall_count or 0)
                            + int(loser.recall_count or 0)
                            + 1  # +1 for the consolidation event itself
                        )
                        survivor.last_recalled_at = datetime.utcnow()
                        archived_ids.add(loser.id)
                        merges.append(
                            {
                                "kind": k,
                                "survivor_id": survivor.id,
                                "archived_id": loser.id,
                                "similarity": round(ratio, 3),
                                "survivor_source": survivor.source,
                                "archived_source": loser.source,
                                "survivor_content": survivor.content,
                                "archived_content": loser.content,
                            }
                        )
                    if considered_pairs > max_pairs:
                        break
        return {
            "pairs_considered": considered_pairs,
            "pairs_merged": len(merges),
            "similarity_threshold": similarity_threshold,
            "details": merges,
        }

    @classmethod
    def _rank_merge_candidates(
        cls, a: UserMemory, b: UserMemory,
    ) -> tuple[Optional[UserMemory], Optional[UserMemory]]:
        """Return ``(survivor, loser)`` per the consolidation policy.

        Both returned ``None`` if the pair cannot be safely merged.
        """
        def key(row: UserMemory) -> tuple:
            return (
                1 if row.pinned else 0,
                cls._SOURCE_PRIORITY.get(str(row.source), 0),
                int(row.recall_count or 0),
                # Newer wins as the very last tiebreak — older entries
                # were probably the user's earlier phrasing of the
                # same fact, the more recent one usually wins on info.
                row.created_at or datetime.min,
                int(row.id or 0),
            )
        ka, kb = key(a), key(b)
        if ka == kb:
            return a, b  # arbitrary but deterministic by id
        return (a, b) if ka > kb else (b, a)
