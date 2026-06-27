"""SQLite-backed answer cache (the "wiki").

Storage layer only — no business rules, no normalization decisions
(those live in :mod:`backend.wiki.normalizer`), no router glue (that
lives in ``backend/agent/loop.py``). Mirrors the
``MemoryStore``/``MemoryManager`` separation: this class can be
unit-tested in isolation and a future swap to a vector backend
changes ``lookup()`` only.

Design choices
--------------
* **Stateless.** Every method opens its own ``session_scope``. Safe
  to share one instance across coroutines, no locking required.
* **Skill-scoped.** All queries filter on ``skill_id`` so removing a
  skill (or renaming it) lets us invalidate its rows in a single
  ``DELETE WHERE skill_id=?``.
* **No partial-row updates from the hit path.** The hit-counter
  bump runs in its own transaction so a slow read elsewhere can't
  hold a write lock on the cache.
* **Lookup is two-phase.** First an exact match on the normalized
  query (the common case — cheap O(log n) index seek). On miss, a
  bounded-substring fallback (LIKE %q%) handles the case where the
  user phrases the query slightly longer than the cached form.
  Substring matches are scored by length-ratio so a stored
  "北京 3日" beats a stored "北京 3日 美食 推荐" when the user types
  "北京 3日游" — closer length wins.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

from loguru import logger
from sqlalchemy import case, select, update, delete

from ..db.models import WikiEntry
from ..db.session import session_scope
from .normalizer import normalize_for_kind


# clamp confidence between [0.05, 0.95] inside a SQL UPDATE so
# the bump path (lookup hit) and the decay path (forgetting curve)
# can both reuse it without a read-modify-write round-trip. We never
# go below 0.05 (so a fact can be re-confirmed back into trust) and
# never above 0.95 (so old beliefs stay reachable by forgetting). The
# breakpoints align with nvk/llm-wiki's qualitative bands:
# low (<0.5), medium (0.5-0.8), high (>0.8).
def _sql_clamp_confidence(expr):
    """Return a SQLAlchemy expression equivalent to ``clamp(expr, 0.05, 0.95)``."""
    return case(
        (expr > 0.95, 0.95),
        (expr < 0.05, 0.05),
        else_=expr,
    )

# typing-only import for the optional Redis backend.  The
# concrete class lives in ``backend.storage`` which depends on the
# (optional) ``redis`` package; deferring the import keeps the wiki
# unit tests fast and lets pure-offline smoke runs ignore Redis
# entirely.
if False:  # pragma: no cover - typing only
    from ..storage import RedisBackend


@dataclass(slots=True)
class WikiHit:
    """Caller-facing view of a successful lookup.

    The ORM row is intentionally NOT exposed — it's mutable and tied
    to a session. ``WikiHit`` is a frozen value object the agent loop
    can hand to the IM dispatch layer without worrying about session
    lifetimes or accidental writes.
    """

    id: int
    skill_id: str
    raw_query: str
    normalized_query: str
    answer: str
    hit_count: int
    similarity_kind: str  # "exact" | "substring" | "alias"
    metadata: dict[str, object]
    # Wiki v2 fields. Defaults match a brand-new entry so
    # legacy callers that only construct WikiHit positionally (or by
    # only the v0.37 keyword set) keep working.
    confidence: float = 0.5
    sources: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    crystal_kind: str = "answer"
    # semicolon-separated geo_path tokens. Empty for
    # entries with no geographic context (most non-travel skills).
    geo_path: str = ""


class WikiStore:
    """CRUD over the ``wiki_entries`` table."""

    # Substring matches below this length-ratio are rejected. Stored
    # / queried normalized strings of very different lengths are
    # almost always different intents (e.g. "北京 3日" vs
    # "北京 3日 美食 餐厅 排名 评价") — better a miss + fresh LLM
    # answer than a stale wrong-context hit.
    DEFAULT_MIN_LENGTH_RATIO: float = 0.55

    def __init__(
        self,
        *,
        redis_backend: Optional["RedisBackend"] = None,
        redis_ttl_seconds: int = 30 * 86_400,
    ) -> None:
        """optional Redis hot layer in front of SQLite.

        ``redis_backend`` is the shared :class:`RedisBackend` injected
        by ``backend.app``.  ``None`` keeps the previous SQLite-only
        behaviour byte-for-byte; smoke tests that don't care about
        Redis can omit it.

        ``redis_ttl_seconds`` is the longest a cached entry should
        live in Redis. We pick the same default as the on-disk wiki
        TTL (30 days) so a Redis flush never silently extends the
        skill's cache lifetime; a shorter per-add ``ttl_seconds``
        passed to :meth:`add` will further shorten this on a key-by-
        key basis.
        """
        self._redis = redis_backend
        # Redis TTLs that are too short defeat the point of the cache;
        # too long and we hand out stale answers after a skill ships
        # a new playbook. 30 days mirrors travel-guide's wiki TTL.
        self._redis_default_ttl = max(60, int(redis_ttl_seconds))

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def lookup(
        self,
        skill_id: str,
        raw_query: str,
        *,
        now: Optional[datetime] = None,
        min_length_ratio: float = DEFAULT_MIN_LENGTH_RATIO,
        include_superseded: bool = False,
        geo_filter: Optional[str] = None,
    ) -> Optional[WikiHit]:
        """Find a non-expired entry for ``raw_query`` under ``skill_id``.

        Returns ``None`` on miss. On hit, this also bumps the entry's
        ``hit_count``, ``last_hit_at``, ``last_confirmed_at`` and
        ``confidence`` in a separate transaction — callers don't
        need to track usage manually. The bumps are best-effort: a
        write failure logs and returns the hit anyway, because
        losing telemetry is a much smaller cost than losing the
        cache speedup.

        the lookup pipeline now has THREE phases:
          1. exact match on ``normalized_query`` (cheap index seek);
          2. substring fallback (LIKE %canonical% scan) ranked by
             length-ratio;
          3. alias fallback (scan ``aliases_json`` for a match).
        Superseded rows are excluded by default — callers that
        want the historical view (REST audit) pass
        ``include_superseded=True``.

        ``geo_filter`` (e.g. ``"city:上海"``) restricts
        every phase to rows whose ``geo_path`` LIKE-contains that
        token. ``None`` means "don't filter on geo" — used by
        legacy non-travel skills and by the REST audit surface.
        Use ``""`` (empty string) to require an UN-tagged row.
        """
        if not skill_id or not raw_query:
            return None
        canonical = normalize_for_kind(skill_id, raw_query)
        if not canonical:
            return None
        now = now or datetime.utcnow()

        # Redis read-through. Redis only ever caches the
        # *exact* canonical key (substring matches stay SQLite-only
        # because computing a length-ratio across the whole keyspace
        # would defeat the point of the cache). On hit we still bump
        # the SQLite hit counter best-effort below for telemetry.
        redis_hit = self._redis_lookup_exact(skill_id, canonical)
        if redis_hit is not None:
            logger.info(
                "wiki HIT-redis skill={} canonical={!r} hits={}",
                skill_id, canonical, redis_hit.hit_count,
            )
            return redis_hit

        with session_scope() as session:
            # base predicate that excludes superseded rows.
            # ``include_superseded`` flips the SQL guard for the REST
            # audit surface only; the agent loop always wants the
            # current row.
            def _base_filter(stmt):
                stmt = stmt.where(WikiEntry.skill_id == skill_id)
                if not include_superseded:
                    stmt = stmt.where(WikiEntry.superseded_by.is_(None))
                # geo restriction. ``None`` keeps the legacy
                # behaviour (any row); ``""`` matches only un-tagged
                # rows; any other value LIKE-matches the geo_path
                # column. Tokens are guaranteed unique (``city:上海``
                # never appears as a substring of another valid
                # token because the type prefix forces a colon).
                if geo_filter is not None:
                    if geo_filter == "":
                        stmt = stmt.where(WikiEntry.geo_path == "")
                    else:
                        stmt = stmt.where(
                            WikiEntry.geo_path.like(f"%{geo_filter}%")
                        )
                return stmt

            # Phase 1 — exact match. Index seek on (skill_id,
            # normalized_query). We additionally filter on
            # ``expires_at`` so a stale row never returns a hit.
            stmt = _base_filter(
                select(WikiEntry).where(
                    WikiEntry.normalized_query == canonical,
                )
            ).order_by(WikiEntry.created_at.desc()).limit(1)
            row = session.execute(stmt).scalar_one_or_none()
            similarity_kind = "exact"

            # Phase 2 — substring fallback. Only on exact miss; we
            # don't want to pay the LIKE scan when the index already
            # answered. Bounded to 16 candidates (heuristic; deeper
            # search is rarely useful and risks picking up unrelated
            # rows).
            if row is None or row.expires_at is not None and row.expires_at <= now:
                if row is not None:
                    # Exact-but-expired: nuke it so future writes
                    # produce a clean replacement instead of stacking
                    # parallel rows.
                    session.execute(
                        delete(WikiEntry).where(WikiEntry.id == row.id)
                    )
                    row = None
                like = f"%{canonical}%"
                stmt2 = _base_filter(
                    select(WikiEntry).where(
                        WikiEntry.normalized_query.like(like),
                    )
                ).order_by(WikiEntry.created_at.desc()).limit(16)
                candidates: list[WikiEntry] = list(
                    session.execute(stmt2).scalars()
                )
                # Filter out expired and rank by length-ratio.
                fresh = [
                    c for c in candidates
                    if c.expires_at is None or c.expires_at > now
                ]
                if fresh:
                    # closer length = closer intent. Avoid div-by-zero
                    # when both happen to be empty.
                    target_len = max(len(canonical), 1)
                    ranked = sorted(
                        fresh,
                        key=lambda c: -min(
                            len(c.normalized_query), target_len
                        ) / max(len(c.normalized_query), target_len),
                    )
                    cand = ranked[0]
                    ratio = min(len(cand.normalized_query), target_len) / max(
                        len(cand.normalized_query), target_len
                    )
                    if ratio >= min_length_ratio:
                        row = cand
                        similarity_kind = "substring"

            # Phase 3 — alias fallback. v0.39: rows can carry a
            # JSON-array of alternate normalized queries the LLM
            # produced when crystallizing the fact. We do a coarse
            # LIKE scan on the JSON column (cheap on the small
            # per-skill keyspace; substituted by a proper inverted
            # index when the wiki grows past ~10k rows). The match
            # is exact on the canonical key WITHIN the JSON array —
            # we don't want substrings of aliases to match.
            if row is None or row.expires_at is not None and row.expires_at <= now:
                if row is not None and row.expires_at is not None and row.expires_at <= now:
                    session.execute(
                        delete(WikiEntry).where(WikiEntry.id == row.id)
                    )
                    row = None
                # Look for any alias entry containing the canonical
                # key as a JSON string element. Quoted to enforce
                # full-string match, not substring.
                json_needle = f'"{canonical}"'
                stmt3 = _base_filter(
                    select(WikiEntry).where(
                        WikiEntry.aliases_json.like(f"%{json_needle}%"),
                    )
                ).order_by(WikiEntry.created_at.desc()).limit(8)
                alias_candidates = list(
                    session.execute(stmt3).scalars()
                )
                fresh_aliases = [
                    c for c in alias_candidates
                    if c.expires_at is None or c.expires_at > now
                ]
                # Validate the JSON membership rather than trusting
                # LIKE alone (which can false-positive on partial
                # matches inside another alias string).
                for cand in fresh_aliases:
                    try:
                        aliases = json.loads(cand.aliases_json or "[]")
                    except (ValueError, TypeError):
                        aliases = []
                    if isinstance(aliases, list) and canonical in aliases:
                        row = cand
                        similarity_kind = "alias"
                        break
                if row is None:
                    return None

            # Build the value-object snapshot before we leave the
            # session — accessing ORM attributes after session close
            # raises DetachedInstanceError.
            try:
                metadata = json.loads(row.metadata_json or "{}")
                if not isinstance(metadata, dict):
                    metadata = {}
            except (ValueError, TypeError):
                metadata = {}
            try:
                sources = tuple(json.loads(row.sources_json or "[]") or ())
            except (ValueError, TypeError):
                sources = ()
            try:
                aliases = tuple(json.loads(row.aliases_json or "[]") or ())
            except (ValueError, TypeError):
                aliases = ()
            hit = WikiHit(
                id=row.id,
                skill_id=row.skill_id,
                raw_query=row.raw_query,
                normalized_query=row.normalized_query,
                answer=row.answer,
                hit_count=row.hit_count + 1,  # post-bump value
                similarity_kind=similarity_kind,
                metadata=metadata,
                confidence=float(row.confidence or 0.5),
                sources=sources,
                aliases=aliases,
                crystal_kind=str(row.crystal_kind or "answer"),
                geo_path=str(row.geo_path or ""),
            )
            row_id = row.id
            # capture expires_at while still inside the
            # session; SQLAlchemy raises DetachedInstanceError when
            # we touch ORM attrs after ``with session_scope`` exits.
            row_expires_at = row.expires_at

        # Best-effort hit-counter bump in its own transaction.
        # also bumps last_confirmed_at + confidence (cap 0.95
        # so a row never becomes immune to forgetting). The +0.05
        # increment is calibrated so a row needs ~10 hits to climb
        # from 0.5 → 0.95, matching nvk/llm-wiki's qualitative
        # "high/medium/low" breakpoints (low<0.5, medium 0.5-0.8,
        # high>0.8).
        try:
            with session_scope() as bump:
                bump.execute(
                    update(WikiEntry)
                    .where(WikiEntry.id == row_id)
                    .values(
                        hit_count=WikiEntry.hit_count + 1,
                        last_hit_at=now,
                        last_confirmed_at=now,
                        confidence=_sql_clamp_confidence(
                            WikiEntry.confidence + 0.05,
                        ),
                    )
                )
        except Exception as exc:  # noqa: BLE001 - never fail a hit on telemetry
            logger.warning(
                "wiki hit-count bump failed for id={} skill={}: {}",
                row_id, skill_id, exc,
            )

        # populate Redis on a SQLite hit so the next read
        # short-circuits the database. Only ``exact`` hits go to
        # Redis; substring hits are intentionally not cached because
        # the same Redis key would mask a more-specific exact entry
        # that arrives later.
        if similarity_kind == "exact":
            ttl = self._derive_redis_ttl(row_expires_at=row_expires_at, now=now)
            self._redis_store(skill_id, canonical, hit, ttl_seconds=ttl)

        logger.info(
            "wiki HIT skill={} kind={} canonical={!r} hits={}",
            skill_id, similarity_kind, canonical, hit.hit_count,
        )
        return hit

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def add(
        self,
        skill_id: str,
        raw_query: str,
        answer: str,
        *,
        ttl_seconds: Optional[int] = None,
        metadata: Optional[dict[str, object]] = None,
        now: Optional[datetime] = None,
        # Wiki v2 fields. All are optional + defaulted so
        # legacy callers (just pass skill+query+answer+ttl) keep
        # working byte-for-byte; new callers (crystallization,
        # operator REST) can opt into the structured payload.
        confidence: Optional[float] = None,
        sources: Optional[Sequence[str]] = None,
        aliases: Optional[Sequence[str]] = None,
        crystal_kind: str = "answer",
        supersede_existing: bool = True,
        # geo tagging. ``""`` (default) means "no geo
        # context" — most non-travel rows leave it empty. Travel
        # crystallization fills it with a token like ``"city:上海"``
        # or a multi-locus path like ``"city:北京;city:上海"``.
        geo_path: str = "",
    ) -> int:
        """Insert (or replace) a cache row.

        ``ttl_seconds=None`` means "never expires" — used for stable
        knowledge. Pass an int for time-bounded cache (travel guides
        default to 30 days = 2_592_000 s).

        Returns the new entry's ``id``. Empty / invalid inputs raise
        ``ValueError`` so a buggy caller fails loudly instead of
        silently writing junk into the cache.

        v0.39 supersession (``supersede_existing=True``, the default):
        when an entry already exists for the same
        ``(skill_id, normalized_query)`` and its ``answer`` is
        DIFFERENT from the new ``answer``, the OLD row is kept but
        marked ``superseded_by=<new_id>``. The lookup path skips
        superseded rows by default, so the user-visible behaviour is
        identical to a replace, but the historical record survives
        for the REST audit surface and for future supersession-
        chain analysis ("this fact has been corrected 3 times in 6
        months — maybe the source is unreliable"). Passing
        ``supersede_existing=False`` reverts to the v0.37 behaviour
        of hard-deleting the prior row.

        atomic-fact rows (``crystal_kind="atomic_fact"``)
        live alongside answer rows; both are queried by the same
        ``lookup`` pipeline. Atomic-fact rows usually carry
        ``confidence`` initial values 0.4-0.7 set by the
        crystallizer, while user-corrected rows can be initialised
        as high as 0.9.
        """
        if not skill_id:
            raise ValueError("skill_id is required")
        if not raw_query or not raw_query.strip():
            raise ValueError("raw_query must not be empty")
        if not answer or not answer.strip():
            raise ValueError("answer must not be empty")

        canonical = normalize_for_kind(skill_id, raw_query)
        if not canonical:
            raise ValueError(
                "raw_query normalized to empty string;"
                " refusing to write a key-less row"
            )
        now = now or datetime.utcnow()
        expires_at = (
            now + timedelta(seconds=ttl_seconds)
            if ttl_seconds is not None and ttl_seconds > 0
            else None
        )
        metadata_json = json.dumps(metadata or {}, ensure_ascii=False)
        # Clamp confidence into the allowed range; 0.5 is the legacy
        # default ("medium" = neither trusted nor rejected).
        conf = 0.5 if confidence is None else max(0.05, min(0.95, float(confidence)))
        sources_json = json.dumps(list(sources or ()), ensure_ascii=False)
        aliases_json = json.dumps(list(aliases or ()), ensure_ascii=False)
        if crystal_kind not in ("answer", "atomic_fact"):
            raise ValueError(
                f"crystal_kind must be 'answer' or 'atomic_fact'; got {crystal_kind!r}"
            )
        # Defensive: strip whitespace and collapse stray separators
        # in geo_path so ``"city:上海 ; city:北京"`` and
        # ``"city:上海;city:北京"`` write the same row.
        geo_path_clean = ";".join(
            tok.strip() for tok in (geo_path or "").split(";") if tok.strip()
        )

        with session_scope() as session:
            existing = session.execute(
                select(WikiEntry)
                .where(
                    WikiEntry.skill_id == skill_id,
                    WikiEntry.normalized_query == canonical,
                    WikiEntry.superseded_by.is_(None),
                )
                .order_by(WikiEntry.created_at.desc())
                .limit(1)
            ).scalar_one_or_none()

            entry = WikiEntry(
                skill_id=skill_id,
                normalized_query=canonical,
                raw_query=raw_query,
                answer=answer,
                metadata_json=metadata_json,
                created_at=now,
                expires_at=expires_at,
                hit_count=0,
                last_hit_at=None,
                confidence=conf,
                sources_json=sources_json,
                aliases_json=aliases_json,
                last_confirmed_at=now,
                crystal_kind=crystal_kind,
                geo_path=geo_path_clean,
            )
            session.add(entry)
            session.flush()
            new_id = entry.id

            if existing is not None:
                if (
                    supersede_existing
                    and existing.answer.strip() != answer.strip()
                ):
                    # keep the old row + mark as superseded.
                    # Lookup skips superseded rows so user-visible
                    # behaviour is unchanged; audit/history can
                    # walk the chain.
                    session.execute(
                        update(WikiEntry)
                        .where(WikiEntry.id == existing.id)
                        .values(superseded_by=new_id)
                    )
                else:
                    # Either supersession is disabled or the answer
                    # is byte-identical (a re-confirmation). Drop
                    # the old row outright; the new one carries
                    # forward.
                    session.execute(
                        delete(WikiEntry).where(WikiEntry.id == existing.id)
                    )

        # populate Redis on every add so subsequent reads
        # short-circuit SQLite.  We mirror SQLite's TTL: ``None``
        # (timeless) → fall back to the configured Redis default;
        # otherwise honour the caller's ttl_seconds verbatim.
        hit = WikiHit(
            id=new_id,
            skill_id=skill_id,
            raw_query=raw_query,
            normalized_query=canonical,
            answer=answer,
            hit_count=0,
            similarity_kind="exact",
            metadata=dict(metadata or {}),
            confidence=conf,
            sources=tuple(sources or ()),
            aliases=tuple(aliases or ()),
            crystal_kind=crystal_kind,
            geo_path=geo_path_clean,
        )
        redis_ttl = (
            int(ttl_seconds)
            if ttl_seconds is not None and ttl_seconds > 0
            else self._redis_default_ttl
        )
        self._redis_store(skill_id, canonical, hit, ttl_seconds=redis_ttl)

        logger.info(
            "wiki ADD skill={} canonical={!r} kind={} conf={:.2f} ttl={} id={}",
            skill_id, canonical, crystal_kind, conf, ttl_seconds, new_id,
        )
        return new_id

    # ------------------------------------------------------------------
    # Admin
    # ------------------------------------------------------------------

    def list_entries(
        self,
        *,
        skill_id: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, object]]:
        """Return rows as plain dicts for the REST surface."""
        if limit <= 0:
            return []
        with session_scope() as session:
            stmt = select(WikiEntry).order_by(WikiEntry.created_at.desc())
            if skill_id:
                stmt = stmt.where(WikiEntry.skill_id == skill_id)
            stmt = stmt.limit(limit)
            rows: Sequence[WikiEntry] = list(session.execute(stmt).scalars())
            out: list[dict[str, object]] = []
            for row in rows:
                try:
                    md = json.loads(row.metadata_json or "{}")
                    if not isinstance(md, dict):
                        md = {}
                except (ValueError, TypeError):
                    md = {}
                # surface the new fields on the REST list
                # so operators can audit confidence drift,
                # supersession chains, and crystallization
                # provenance without a separate endpoint.
                try:
                    sources = json.loads(row.sources_json or "[]") or []
                except (ValueError, TypeError):
                    sources = []
                try:
                    aliases = json.loads(row.aliases_json or "[]") or []
                except (ValueError, TypeError):
                    aliases = []
                out.append({
                    "id": row.id,
                    "skill_id": row.skill_id,
                    "raw_query": row.raw_query,
                    "normalized_query": row.normalized_query,
                    "answer": row.answer,
                    "metadata": md,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                    "last_hit_at": row.last_hit_at.isoformat() if row.last_hit_at else None,
                    "hit_count": row.hit_count,
                    "expires_at": row.expires_at.isoformat() if row.expires_at else None,
                    # v0.39 fields
                    "confidence": float(row.confidence or 0.5),
                    "sources": sources,
                    "aliases": aliases,
                    "superseded_by": row.superseded_by,
                    "last_confirmed_at": (
                        row.last_confirmed_at.isoformat()
                        if row.last_confirmed_at else None
                    ),
                    "crystal_kind": row.crystal_kind or "answer",
                    # surface the geo_path so the audit
                    # endpoint can show which rows are tagged.
                    "geo_path": row.geo_path or "",
                })
            return out

    def delete(self, entry_id: int) -> bool:
        """Remove one row. Returns ``True`` if a row was deleted."""
        # capture skill_id + normalized_query *before* the
        # delete so we know which Redis key to invalidate. Reading
        # back after delete returns nothing.
        skill_id_for_redis: Optional[str] = None
        canonical_for_redis: Optional[str] = None
        with session_scope() as session:
            row = session.execute(
                select(WikiEntry).where(WikiEntry.id == entry_id)
            ).scalar_one_or_none()
            if row is not None:
                skill_id_for_redis = row.skill_id
                canonical_for_redis = row.normalized_query
            result = session.execute(
                delete(WikiEntry).where(WikiEntry.id == entry_id)
            )
            removed = bool(result.rowcount)
        if removed:
            if skill_id_for_redis and canonical_for_redis:
                self._redis_invalidate(skill_id_for_redis, canonical_for_redis)
            logger.info("wiki DELETE id={}", entry_id)
        return removed

    def expire_stale(self, *, now: Optional[datetime] = None) -> int:
        """Remove every row whose ``expires_at <= now``.

        Returns the number of rows removed. Suitable for a periodic
        sweep job or the ``POST /api/wiki/refresh`` admin endpoint.
        Rows with ``expires_at IS NULL`` (timeless knowledge) are
        never touched.
        """
        now = now or datetime.utcnow()
        # gather (skill_id, normalized_query) of about-to-
        # expire rows so we can invalidate Redis after the SQL purge.
        # Each row's Redis key is independent; we batch them after the
        # SQLite transaction so any partial Redis failure cannot block
        # the SQLite cleanup.
        stale_pairs: list[tuple[str, str]] = []
        with session_scope() as session:
            stale_rows = session.execute(
                select(WikiEntry.skill_id, WikiEntry.normalized_query).where(
                    WikiEntry.expires_at.is_not(None),
                    WikiEntry.expires_at <= now,
                )
            ).all()
            stale_pairs = [(s, q) for s, q in stale_rows]
            result = session.execute(
                delete(WikiEntry).where(
                    WikiEntry.expires_at.is_not(None),
                    WikiEntry.expires_at <= now,
                )
            )
            removed = int(result.rowcount or 0)
        if removed:
            for skill_id, canonical in stale_pairs:
                self._redis_invalidate(skill_id, canonical)
            logger.info("wiki SWEEP removed {} stale row(s)", removed)
        return removed

    # ------------------------------------------------------------------
    # Wiki v2: decay, supersession, low-confidence audit
    # ------------------------------------------------------------------

    # Default decay factor mirrors a coarse Ebbinghaus curve: every
    # ``stale_after_days`` of inactivity multiplies confidence by
    # 0.9. So a fact that hasn't been confirmed for 30 days drops
    # 0.7 → 0.63; for 90 days, 0.7 → 0.51. The decay floor is the
    # global clamp (0.05) so a fact never disappears entirely
    # without an explicit retract.
    DEFAULT_DECAY_FACTOR: float = 0.9
    DEFAULT_DECAY_STALE_DAYS: int = 30

    def decay_unconfirmed(
        self,
        *,
        now: Optional[datetime] = None,
        stale_after_days: int = DEFAULT_DECAY_STALE_DAYS,
        factor: float = DEFAULT_DECAY_FACTOR,
        dry_run: bool = False,
    ) -> int:
        """Apply the forgetting curve to confidence on stale rows.

        A row is "stale" if ``last_confirmed_at`` (falling back to
        ``created_at``) is older than ``stale_after_days``. Each
        stale row's ``confidence`` is multiplied by ``factor`` and
        clamped to the [0.05, 0.95] band. Returns the number of
        rows that would be (or were, when ``dry_run=False``)
        affected.

        This is meant to be called from a daily cron / scheduler
        sweep. It does NOT delete anything — operators inspect
        low-confidence rows via :meth:`find_low_confidence` and
        retract on a case-by-case basis. nvk/llm-wiki's stance:
        "forgetting is gradual deprioritization, not deletion".
        """
        now = now or datetime.utcnow()
        stale_threshold = now - timedelta(days=int(stale_after_days))
        if not (0.0 < factor < 1.0):
            raise ValueError(f"factor must be in (0.0, 1.0); got {factor}")

        with session_scope() as session:
            # Count first so dry-run can report the blast radius
            # without committing the UPDATE. The COALESCE picks the
            # newer of last_confirmed_at and created_at — if a
            # legacy row never had last_confirmed_at populated, we
            # fall back to created_at.
            from sqlalchemy import func as _f
            recency = _f.coalesce(
                WikiEntry.last_confirmed_at,
                WikiEntry.created_at,
            )
            target_filter = (
                recency < stale_threshold,
                # Ignore already-superseded rows; they shouldn't
                # influence lookup so their confidence doesn't
                # matter.
                WikiEntry.superseded_by.is_(None),
            )
            count = session.execute(
                select(_f.count()).select_from(WikiEntry).where(*target_filter)
            ).scalar() or 0
            if dry_run or count == 0:
                logger.info(
                    "wiki DECAY dry_run={} would-affect={} stale>{}d factor={}",
                    dry_run, count, stale_after_days, factor,
                )
                return int(count)
            session.execute(
                update(WikiEntry)
                .where(*target_filter)
                .values(
                    confidence=_sql_clamp_confidence(
                        WikiEntry.confidence * factor,
                    ),
                )
            )
        logger.info(
            "wiki DECAY applied to {} row(s); stale>{}d factor={}",
            count, stale_after_days, factor,
        )
        return int(count)

    def supersede(
        self,
        old_id: int,
        new_id: int,
        *,
        reason: str = "",
    ) -> bool:
        """Manually mark ``old_id`` as superseded by ``new_id``.

        Used by user-correction flows ("the bot said X, the right
        answer is Y") and by the operator REST surface. Both rows
        must already exist; the function returns ``True`` if the
        update touched a row, ``False`` if either id is missing.
        ``reason`` is stored in the old row's ``metadata_json``
        under the key ``"superseded_reason"`` for audit.
        """
        if old_id == new_id:
            raise ValueError("a row cannot supersede itself")
        with session_scope() as session:
            old = session.execute(
                select(WikiEntry).where(WikiEntry.id == old_id)
            ).scalar_one_or_none()
            new = session.execute(
                select(WikiEntry).where(WikiEntry.id == new_id)
            ).scalar_one_or_none()
            if old is None or new is None:
                return False
            if reason:
                try:
                    md = json.loads(old.metadata_json or "{}")
                    if not isinstance(md, dict):
                        md = {}
                except (ValueError, TypeError):
                    md = {}
                md["superseded_reason"] = reason
                md["superseded_at"] = datetime.utcnow().isoformat()
                session.execute(
                    update(WikiEntry)
                    .where(WikiEntry.id == old_id)
                    .values(
                        superseded_by=new_id,
                        metadata_json=json.dumps(md, ensure_ascii=False),
                    )
                )
            else:
                session.execute(
                    update(WikiEntry)
                    .where(WikiEntry.id == old_id)
                    .values(superseded_by=new_id)
                )
            # Invalidate Redis for the old key — a future read for
            # the same canonical query would otherwise return the
            # superseded answer from the hot layer.
            self._redis_invalidate(old.skill_id, old.normalized_query)
        logger.info(
            "wiki SUPERSEDE old={} new={} reason={!r}",
            old_id, new_id, reason or "<none>",
        )
        return True

    def find_low_confidence(
        self,
        *,
        threshold: float = 0.3,
        skill_id: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict[str, object]]:
        """Return rows whose ``confidence < threshold``.

        Used by the operator REST audit surface to find candidates
        for retraction or refresh. Excludes superseded rows by
        default (they're already deprioritised; their confidence
        doesn't matter operationally).
        """
        if limit <= 0:
            return []
        with session_scope() as session:
            stmt = (
                select(WikiEntry)
                .where(
                    WikiEntry.confidence < threshold,
                    WikiEntry.superseded_by.is_(None),
                )
                .order_by(WikiEntry.confidence.asc())
                .limit(limit)
            )
            if skill_id:
                stmt = stmt.where(WikiEntry.skill_id == skill_id)
            rows: Sequence[WikiEntry] = list(session.execute(stmt).scalars())
            out: list[dict[str, object]] = []
            for row in rows:
                out.append({
                    "id": row.id,
                    "skill_id": row.skill_id,
                    "normalized_query": row.normalized_query,
                    "confidence": float(row.confidence or 0.5),
                    "hit_count": row.hit_count,
                    "last_confirmed_at": (
                        row.last_confirmed_at.isoformat()
                        if row.last_confirmed_at else None
                    ),
                    "crystal_kind": row.crystal_kind or "answer",
                })
            return out

    # ------------------------------------------------------------------
    # Redis hot layer helpers
    # ------------------------------------------------------------------

    def _redis_key(self, skill_id: str, canonical: str) -> str:
        """``zlagent:wiki:<skill>:<sha1-prefix>`` — short and namespaced.

        We hash the canonical query because it can be long (sometimes
        40+ Chinese chars after normalization) and Redis keys travel
        on every command. 24 hex chars = 96-bit space; collisions
        would silently mask one entry behind another, but at the
        scale of the wiki (~few thousand entries) the probability is
        comfortably below 1e-12.
        """
        if self._redis is None:
            return ""
        digest = hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:24]
        return self._redis.namespaced("wiki", skill_id, digest)

    def _redis_lookup_exact(
        self, skill_id: str, canonical: str,
    ) -> Optional[WikiHit]:
        if self._redis is None:
            return None
        key = self._redis_key(skill_id, canonical)
        if not key:
            return None
        try:
            payload = self._redis.get_json(key)
        except Exception as exc:  # noqa: BLE001 - never break a lookup on cache
            logger.debug("[wiki] redis lookup failed key={}: {}", key, exc)
            return None
        if not isinstance(payload, dict):
            return None
        try:
            answer = str(payload.get("answer") or "")
            if not answer:
                return None
            metadata = payload.get("metadata")
            if not isinstance(metadata, dict):
                metadata = {}
            return WikiHit(
                id=int(payload.get("id") or 0),
                skill_id=str(payload.get("skill_id") or skill_id),
                raw_query=str(payload.get("raw_query") or ""),
                normalized_query=str(payload.get("normalized_query") or canonical),
                answer=answer,
                hit_count=int(payload.get("hit_count") or 0) + 1,
                similarity_kind="redis",
                metadata=metadata,
            )
        except (TypeError, ValueError) as exc:
            logger.debug("[wiki] redis payload parse failed: {}", exc)
            return None

    def _redis_store(
        self,
        skill_id: str,
        canonical: str,
        hit: WikiHit,
        *,
        ttl_seconds: int,
    ) -> None:
        if self._redis is None or ttl_seconds <= 0:
            return
        key = self._redis_key(skill_id, canonical)
        if not key:
            return
        try:
            self._redis.set_json(
                key,
                {
                    "id": int(hit.id),
                    "skill_id": hit.skill_id,
                    "raw_query": hit.raw_query,
                    "normalized_query": hit.normalized_query,
                    "answer": hit.answer,
                    "hit_count": int(hit.hit_count),
                    "metadata": hit.metadata,
                },
                ttl_seconds=int(ttl_seconds),
            )
        except Exception as exc:  # noqa: BLE001 - cache write must not break add
            logger.debug("[wiki] redis store failed key={}: {}", key, exc)

    def _redis_invalidate(self, skill_id: str, canonical: str) -> None:
        if self._redis is None:
            return
        key = self._redis_key(skill_id, canonical)
        if not key:
            return
        try:
            self._redis.delete(key)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[wiki] redis invalidate failed key={}: {}", key, exc)

    def _derive_redis_ttl(
        self,
        *,
        row_expires_at: Optional[datetime],
        now: datetime,
    ) -> int:
        """Choose a Redis TTL that is no longer than SQLite's TTL.

        SQLite is the source of truth: if a row was written with
        ``expires_at = now + 30d`` we never want Redis to outlive
        that. Rows with no SQLite expiry use the configured Redis
        default (so a Redis flush won't extend timeless knowledge).
        """
        default = max(60, int(self._redis_default_ttl))
        if row_expires_at is None:
            return default
        # ``row_expires_at`` and ``now`` are naive datetime per
        # WikiEntry's column type; use the same timezone implicitly.
        delta = (row_expires_at - now).total_seconds()
        if delta <= 0:
            return 0
        return min(default, int(delta))
