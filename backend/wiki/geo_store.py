"""Geographic ontology store.

Loads ``backend/wiki/seeds/geo/*.json`` into the ``geo_entities``
table at startup, then exposes a thin query API (``find_by_name``,
``children_of``, ``ancestors_of``, ``geo_path_for``) that the wiki
agent uses to tag and filter rows.

Design notes:

* **JSON is the source of truth, SQLite is the index.** Editing the
  JSON files and restarting ZLAgent is the only supported way to
  change the ontology — no in-process write API. This keeps the
  data git-trackable and avoids the "did the agent corrupt my
  geography?" failure mode.
* **Upsert semantics on seed.** ``seed_from_json`` looks up by
  ``code`` (or by ``(name, type)`` when code is missing) and updates
  in place. Removed rows in JSON do NOT delete from the DB — that's
  too dangerous for an automatic startup task. Manually run
  ``GeoStore.wipe_all()`` if you really mean to reset.
* **Read-mostly.** A typical query goes through the cached
  ``_name_index`` dict in O(1); the SQLite layer is only touched
  for hierarchy queries and for cross-process consistency.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import GeoEntity


# Where the seed JSON files live. Resolved relative to this file so
# the layout is portable across Windows / Linux / Docker.
_SEED_DIR = Path(__file__).resolve().parent / "seeds" / "geo"


@dataclass(slots=True)
class GeoNode:
    """In-process projection of a ``GeoEntity`` row.

    We don't pass ORM objects out of the store because callers
    typically work outside an active ``Session`` (e.g. the wiki
    crystallizer runs in a fire-and-forget asyncio task). A frozen
    dataclass keeps everything thread-safe and easy to log.
    """

    id: int
    code: Optional[str]
    name: str
    short_name: str
    type: str  # country | province | city | district
    level: int
    parent_id: Optional[int]
    aliases: tuple[str, ...]
    latitude: Optional[float]
    longitude: Optional[float]


class GeoStore:
    """Thin facade over the ``geo_entities`` SQLAlchemy table."""

    def __init__(self, session_factory) -> None:
        # session_factory is the same callable used by the rest of
        # ZLAgent (a sessionmaker bound to the wiki engine). We
        # never hold a session across calls.
        self._session_factory = session_factory
        # In-memory index built on first use, invalidated after every
        # mutation that this class performs (only ``seed_from_json``
        # for now). Keeps name → list[GeoNode] for fast NER lookup.
        self._name_index: Optional[dict[str, list[GeoNode]]] = None

    # ── seeding ────────────────────────────────────────────────────────

    def seed_from_json(self, seed_dir: Optional[Path] = None) -> dict[str, int]:
        """Upsert all rows from ``provinces.json`` / ``cities.json``.

        Returns a small report dict ``{"inserted": N, "updated": M,
        "skipped_no_parent": K, "files": L}`` for the boot log.

        Idempotent: running it twice is a no-op assuming the JSON
        files haven't changed. Safe to call from ``app.lifespan``.
        """
        seed_dir = seed_dir or _SEED_DIR
        report = {"inserted": 0, "updated": 0, "skipped_no_parent": 0, "files": 0}
        if not seed_dir.exists():
            logger.warning("[geo] seed dir missing: {}", seed_dir)
            return report

        # Order matters: provinces FIRST so cities/districts can
        # resolve their ``parent_code``. We hard-code the order
        # rather than relying on lexical sort because the file names
        # don't naturally sort by hierarchy.
        ordered_files = ["provinces.json", "cities.json", "districts.json"]
        with self._session_factory() as session:
            session: Session  # type: ignore[no-redef]
            for fname in ordered_files:
                path = seed_dir / fname
                if not path.exists():
                    continue
                report["files"] += 1
                try:
                    rows = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    logger.error("[geo] failed to read {}: {}", path, exc)
                    continue
                if not isinstance(rows, list):
                    logger.error("[geo] {} is not a JSON array; skipping", path)
                    continue
                for raw in rows:
                    out = self._upsert_one(session, raw)
                    if out == "inserted":
                        report["inserted"] += 1
                    elif out == "updated":
                        report["updated"] += 1
                    elif out == "skipped_no_parent":
                        report["skipped_no_parent"] += 1
                # Flush after each file so the next file's parent_code
                # lookups can see rows we just inserted. The shared
                # SessionLocal is autoflush=False, so without this the
                # cities/districts batch would race against unflushed
                # province inserts and skip every row with
                # ``parent <code> missing``.
                session.flush()
            session.commit()
        # Invalidate the cache so next read sees the new rows.
        self._name_index = None
        logger.info("[geo] seed complete: {}", report)
        return report

    def _upsert_one(self, session: Session, raw: dict) -> str:
        """Insert or update one row. Returns the action taken."""
        code = (raw.get("code") or "").strip() or None
        name = (raw.get("name") or "").strip()
        type_ = (raw.get("type") or "").strip()
        if not name or type_ not in {"country", "province", "city", "district"}:
            logger.warning("[geo] skipping malformed row: {}", raw)
            return "skipped_no_parent"

        parent_code = (raw.get("parent_code") or "").strip() or None
        parent_id: Optional[int] = None
        if parent_code:
            parent = session.execute(
                select(GeoEntity).where(GeoEntity.code == parent_code)
            ).scalar_one_or_none()
            if parent is None:
                # Parent not yet seeded — caller should have ordered
                # files province → city → district. Skip rather than
                # creating a dangling row.
                logger.warning(
                    "[geo] parent {} missing for {} ({}); skipping",
                    parent_code, name, type_,
                )
                return "skipped_no_parent"
            parent_id = parent.id

        level_map = {"country": 0, "province": 1, "city": 2, "district": 3}
        level = level_map[type_]
        aliases = raw.get("aliases") or []
        if not isinstance(aliases, list):
            aliases = []
        short_name = (raw.get("short_name") or name).strip()
        latitude = raw.get("latitude")
        longitude = raw.get("longitude")

        # Look up by code first; fall back to (name, type) when code
        # is missing. This handles user-added entries without an
        # official GB/T 2260 code.
        if code:
            existing = session.execute(
                select(GeoEntity).where(GeoEntity.code == code)
            ).scalar_one_or_none()
        else:
            existing = session.execute(
                select(GeoEntity).where(
                    GeoEntity.name == name,
                    GeoEntity.type == type_,
                )
            ).scalar_one_or_none()

        if existing is None:
            session.add(GeoEntity(
                code=code,
                name=name,
                short_name=short_name,
                type=type_,
                level=level,
                parent_id=parent_id,
                aliases_json=json.dumps(aliases, ensure_ascii=False),
                latitude=latitude,
                longitude=longitude,
            ))
            return "inserted"
        # Update mutable fields in place. We don't change ``code``
        # on an existing row even if the JSON differs — that would
        # break referential integrity for any wiki_entries already
        # tagged with this entity.
        existing.name = name
        existing.short_name = short_name
        existing.level = level
        existing.parent_id = parent_id
        existing.aliases_json = json.dumps(aliases, ensure_ascii=False)
        if latitude is not None:
            existing.latitude = latitude
        if longitude is not None:
            existing.longitude = longitude
        return "updated"

    # ── reads ──────────────────────────────────────────────────────────

    def _ensure_name_index(self) -> dict[str, list[GeoNode]]:
        """Build (or reuse) the in-memory name → nodes index.

        Returns a dict where each key is a name OR alias OR
        short_name, and the value is the list of nodes that match.
        Multi-match is rare but possible (e.g. there are two 朝阳
        districts in different provinces).
        """
        if self._name_index is not None:
            return self._name_index
        index: dict[str, list[GeoNode]] = {}
        with self._session_factory() as session:
            session: Session  # type: ignore[no-redef]
            for row in session.execute(select(GeoEntity)).scalars():
                node = self._row_to_node(row)
                for key in (node.name, node.short_name, *node.aliases):
                    if not key:
                        continue
                    index.setdefault(key, []).append(node)
        self._name_index = index
        return index

    @staticmethod
    def _row_to_node(row: GeoEntity) -> GeoNode:
        try:
            aliases = tuple(json.loads(row.aliases_json or "[]"))
        except (ValueError, TypeError):
            aliases = ()
        return GeoNode(
            id=row.id,
            code=row.code,
            name=row.name,
            short_name=row.short_name or row.name,
            type=row.type,
            level=row.level,
            parent_id=row.parent_id,
            aliases=aliases,
            latitude=row.latitude,
            longitude=row.longitude,
        )

    def find_by_name(
        self,
        token: str,
        *,
        type_: Optional[str] = None,
    ) -> list[GeoNode]:
        """Resolve a free-form token to one or more geo nodes.

        Matches against full ``name``, ``short_name``, and any
        ``aliases``. If ``type_`` is set, narrows the result to that
        level (e.g. ``type_="city"`` skips province-level matches).
        Empty ``token`` returns ``[]``.

        Used by the geo-NER pass when crystallizing wiki entries:
        given the user query "上海到北京高铁", this returns
        ``[GeoNode(上海市), GeoNode(北京市)]``.
        """
        token = (token or "").strip()
        if not token:
            return []
        index = self._ensure_name_index()
        out = index.get(token, [])
        if type_:
            out = [n for n in out if n.type == type_]
        return list(out)

    def children_of(self, parent: GeoNode) -> list[GeoNode]:
        """List the direct children of ``parent`` (one level down).

        E.g. ``children_of(province:上海市)`` returns the cities
        directly under it. Returns ``[]`` for leaves.
        """
        with self._session_factory() as session:
            session: Session  # type: ignore[no-redef]
            rows = session.execute(
                select(GeoEntity).where(GeoEntity.parent_id == parent.id)
            ).scalars().all()
        return [self._row_to_node(r) for r in rows]

    def ancestors_of(self, node: GeoNode) -> list[GeoNode]:
        """Return the chain ``[parent, grandparent, ...]`` (root last).

        Used to build the ``geo_path`` string for a wiki entry: walk
        up to the country root and concatenate ``type:name`` tokens.
        Bounded to 8 levels to defend against accidental cycles in
        seed data; in practice the China admin-division tree is at
        most 4 levels deep.
        """
        chain: list[GeoNode] = []
        cursor = node
        with self._session_factory() as session:
            session: Session  # type: ignore[no-redef]
            for _ in range(8):
                if cursor.parent_id is None:
                    break
                row = session.execute(
                    select(GeoEntity).where(GeoEntity.id == cursor.parent_id)
                ).scalar_one_or_none()
                if row is None:
                    break
                cursor = self._row_to_node(row)
                chain.append(cursor)
        return chain

    def geo_path_for(self, node: GeoNode) -> str:
        """Build the canonical ``geo_path`` token for ``node``.

        Format: ``<type>:<short_name>`` (single token), e.g.
        ``"city:上海"``. The wiki uses ``short_name`` rather than
        ``name`` so user queries like "上海" / "上海市" both
        normalize to the same path. To represent a multi-locus
        fact ("京沪高铁"), the caller joins multiple tokens with
        ``;``.
        """
        return f"{node.type}:{node.short_name}"

    def encode_geo_path(self, nodes: Sequence[GeoNode]) -> str:
        """Join multiple nodes into the wiki ``geo_path`` string.

        Order is preserved so a caller can express directionality
        ("origin first, destination second") if they care, but the
        LIKE query path treats all tokens symmetrically.
        Deduplicates by ``(type, short_name)`` pairs.
        """
        seen: set[str] = set()
        out: list[str] = []
        for n in nodes:
            tok = self.geo_path_for(n)
            if tok in seen:
                continue
            seen.add(tok)
            out.append(tok)
        return ";".join(out)

    def detect_in_text(
        self,
        text: str,
        *,
        max_matches: int = 6,
    ) -> list[GeoNode]:
        """Greedy substring scan for known geo names in ``text``.

        Returns the list of ``GeoNode`` whose ``name`` /
        ``short_name`` / aliases appear in ``text``, longest match
        first. Capped at ``max_matches`` to avoid pathological cases
        (a long answer mentioning every province).

        This is deliberately dumb — no morphological analysis, no
        NER model. The geo names are short and distinctive enough
        that simple substring matching catches the common cases
        (北京, 上海, 杭州, …) without false positives. Edge cases
        (the surname 张  / the common word 一带 containing 一带)
        are accepted as cost of simplicity.
        """
        if not text:
            return []
        index = self._ensure_name_index()
        # Sort keys longest-first so "西双版纳" wins over "版纳"
        # when both would match. Same approach as classical
        # max-match Chinese tokenization.
        keys = sorted(index.keys(), key=len, reverse=True)
        matched: list[GeoNode] = []
        seen_ids: set[int] = set()
        for key in keys:
            if len(matched) >= max_matches:
                break
            if not key or len(key) < 2:
                continue  # 1-char aliases (京/沪/苏) cause too many false positives
            if key in text:
                for node in index[key]:
                    if node.id in seen_ids:
                        continue
                    seen_ids.add(node.id)
                    matched.append(node)
                    if len(matched) >= max_matches:
                        break
        return matched

    # ── ops helpers ────────────────────────────────────────────────────

    def list_all(self, *, type_: Optional[str] = None) -> list[GeoNode]:
        """Return every entity, optionally filtered by ``type_``."""
        with self._session_factory() as session:
            session: Session  # type: ignore[no-redef]
            stmt = select(GeoEntity).order_by(GeoEntity.level, GeoEntity.code)
            if type_:
                stmt = stmt.where(GeoEntity.type == type_)
            rows = session.execute(stmt).scalars().all()
        return [self._row_to_node(r) for r in rows]

    def wipe_all(self) -> int:
        """Delete every row. Returns count deleted. Caller's responsibility."""
        with self._session_factory() as session:
            session: Session  # type: ignore[no-redef]
            count = session.query(GeoEntity).count()
            session.query(GeoEntity).delete()
            session.commit()
        self._name_index = None
        return count
