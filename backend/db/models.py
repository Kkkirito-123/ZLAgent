"""ORM models for ZLAgent.

Only the minimum tables required by the v0.1 skeleton are defined here.
Additional tables (messages, audit logs, permissions, users) will be added in
subsequent iterations so we don't freeze the schema prematurely.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


def _utcnow() -> datetime:
    return datetime.utcnow()


class Skill(Base):
    """A reusable workflow/capability the agent can load.

    Skills are stored as files on disk under ``workspace/skills`` and this
    table only keeps lightweight metadata for listing, usage counters and
    lifecycle tracking.
    """

    __tablename__ = "skills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(32), default="user")  # user | agent | harness
    origin: Mapped[str] = mapped_column(String(64), default="foreground")
    state: Mapped[str] = mapped_column(String(16), default="active")  # active|stale|archived
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    use_count: Mapped[int] = mapped_column(Integer, default=0)
    patch_count: Mapped[int] = mapped_column(Integer, default=0)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    absorbed_into: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow
    )


class DeliveryTarget(Base):
    """A normalized recipient for outgoing messages.

    Any channel adapter (QQ, WeCom, Feishu, email, Telegram, …) speaks to the
    agent through the same DeliveryTarget abstraction so cron jobs and skills
    are not tied to a specific platform.
    """

    __tablename__ = "delivery_targets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform: Mapped[str] = mapped_column(String(32), index=True)
    target_type: Mapped[str] = mapped_column(String(16))  # user|group|channel|email
    target_id: Mapped[str] = mapped_column(String(256))
    display_name: Mapped[str] = mapped_column(String(256), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class CronJob(Base):
    """A scheduled task that runs a skill/command and delivers the result.

    From v0.10: ``pre_script_path`` lets the job run a workspace-local
    subprocess just before the LLM is invoked; the script's stdout is
    spliced into the instruction as fresh data. This is what lets a
    Hermes-style "arxiv daily" / "github stars monitor" skill operate
    on **real** data rather than the LLM's hallucinated guesses.
    """

    __tablename__ = "cron_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    cron_expr: Mapped[str] = mapped_column(String(64))
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai")
    instruction: Mapped[str] = mapped_column(Text, default="")
    skill_hint: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    delivery_target_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    run_once: Mapped[bool] = mapped_column(Boolean, default=False)
    lead_minutes: Mapped[int] = mapped_column(Integer, default=2)
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # optional pre-execution data fetcher.
    pre_script_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    pre_script_timeout_seconds: Mapped[int] = mapped_column(Integer, default=30)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow
    )


class PendingConfirmation(Base):
    """A paused tool invocation waiting for user approval (v0.6).

    When the agent loop hits a ``confirm``-tier tool, instead of executing
    immediately it persists the entire LLM history alongside the pending
    tool call here, sends an IM question to the user, and exits the turn.
    The user's next inbound message — if classified as yes/no by
    :func:`backend.agent.confirmation.classify_decision` resolves this row
    and resumes the agent loop from where it stopped.

    Fields:

    * ``platform`` / ``user_id``: identify *who* must answer. Combined with
      ``status='pending'`` they form a (logical) unique pair — see
      :class:`backend.db.confirmations.ConfirmationStore`.
    * ``reply_target_json``: serialized ``DeliveryTarget`` so the resume
      path can send the eventual answer back through the same IM channel.
    * ``llm_history_json``: the full :class:`LLMMessage` list (system + user
      + assistant + tool messages) up to and including the assistant turn
      that emitted this tool_call. Resume re-hydrates from here.
    * ``tool_name`` + ``tool_arguments_json`` + ``tool_call_id``: identify
      the specific tool invocation so we execute exactly what the LLM asked
      for, with the same ``tool_call_id`` the model is expecting back.
    * ``status``: ``pending`` → ``approved`` / ``denied`` / ``expired`` /
      ``resolved`` (terminal). ``resolved_at`` is set on any non-pending
      transition for auditability.
    """

    __tablename__ = "pending_confirmations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform: Mapped[str] = mapped_column(String(32), index=True)
    user_id: Mapped[str] = mapped_column(String(256), index=True)
    reply_target_json: Mapped[str] = mapped_column(Text)
    tool_name: Mapped[str] = mapped_column(String(128))
    tool_arguments_json: Mapped[str] = mapped_column(Text)
    tool_call_id: Mapped[str] = mapped_column(String(128))
    llm_history_json: Mapped[str] = mapped_column(Text)
    system_prompt: Mapped[str] = mapped_column(Text, default="")
    question_text: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(
        String(16), default="pending", index=True
    )  # pending|approved|denied|expired|resolved
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    resolved_outcome: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class UserMemory(Base):
    """v0.12: cross-session declarative / procedural memory.

    Single-user mode (the operator's own personal-assistant deployment),
    so we don't shard by ``(platform, user_id)`` the way Hermes' multi-
    seat builtin provider does. Adding a user dimension later is a one-
    column migration through :func:`apply_lightweight_migrations`.

    Two semantic ``kind`` values mirror Hermes' MEMORY.md / USER.md:

    * ``user_fact``    — about the operator (preferences, relationships,
                         goals). Equivalent to Hermes' USER.md.
    * ``agent_note``   — agent's own notes (environment quirks, project
                         conventions, learned pitfalls). Equivalent to
                         Hermes' MEMORY.md.

    ``knowledge_base_id`` separates independent memory / knowledge pools
    (for example ``paper`` vs ``travel``) so graph generation and lookup can
    stay isolated by default.

    ``source`` records who wrote the entry: ``explicit`` (the user told
    the agent to remember it via ``memory_manage`` in a foreground turn),
    ``review`` (the v0.9 background review fork wrote it after spotting
    something worth keeping), or ``import`` (REST API or seed script).
    The curator-equivalent rules apply: only ``review``-sourced entries
    are eligible for autonomous trimming; ``explicit`` ones are sacred
    until the user removes them.
    """

    __tablename__ = "user_memories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    knowledge_base_id: Mapped[str] = mapped_column(String(64), index=True, default="default")
    kind: Mapped[str] = mapped_column(String(32), index=True, default="user_fact")
    content: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(16), default="explicit")
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    archived: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    recall_count: Mapped[int] = mapped_column(Integer, default=0)
    last_recalled_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow,
    )


class MCPServerRegistration(Base):
    """runtime-attached MCP server config.

    The static seed for MCP servers still lives in
    ``config/mcp_servers.yaml`` (operator-edited, requires restart). This
    table is the dynamic overlay: every server attached via the
    ``mcp_manage`` IM tool ends up here so it survives a process restart
    without the operator having to mirror the change into YAML.

    Source semantics (the ``source`` column):

    * ``yaml``     — static seed; never written to this table (kept for
                     symmetry with the in-memory representation only).
    * ``im``       — added through the ``mcp_manage`` agent tool.
    * ``rest``     — reserved for the future ``POST /api/mcp/servers``
                     admin endpoint.

    JSON columns (args / env / headers) keep this Python-friendly without
    pulling in a JSON-1 dependency: load with :func:`json.loads`, store
    with :func:`json.dumps`. Empty string is an alias for ``[]`` / ``{}``.
    """

    __tablename__ = "mcp_servers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    transport: Mapped[str] = mapped_column(String(8), default="stdio")
    command: Mapped[str] = mapped_column(String(512), default="")
    args_json: Mapped[str] = mapped_column(Text, default="[]")
    env_json: Mapped[str] = mapped_column(Text, default="{}")
    url: Mapped[str] = mapped_column(String(1024), default="")
    headers_json: Mapped[str] = mapped_column(Text, default="{}")
    description: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # v1.1.0 — per-tool permission overrides (e.g. {"geocode": "safe"}).
    # JSON-encoded dict[str, "safe" | "confirm"]. Default '{}' covers
    # rows created before v1.1.0 — they keep the existing
    # confirm-everywhere behaviour until the operator runs ``promote``.
    tool_override_permission_json: Mapped[str] = mapped_column(
        Text, default="{}",
    )
    source: Mapped[str] = mapped_column(String(16), default="im", index=True)
    created_by: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow
    )


def create_all(engine) -> None:
    """Create all tables if they do not yet exist."""
    Base.metadata.create_all(bind=engine)


class WikiEntry(Base):
    """semantic answer cache for query-type skills.

    A wiki entry is a (skill_id, normalized_query) → answer pairing
    that lets the agent skip the entire LLM round-trip on repeated
    queries. The cache key is the *normalized* query, not the raw
    user text, so paraphrases like "北京三日游" / "去北京玩三天"
    converge to the same row after ``backend/wiki/normalizer.py``
    runs.

    Lifecycle:

    * **Miss path** — wiki.lookup returns None → run_turn does the
      full agent loop → on success an async task writes a row here.
    * **Hit path** — wiki.lookup returns a row whose ``expires_at``
      is in the future → run_turn returns ``answer`` immediately,
      bumping ``hit_count`` and ``last_hit_at``.
    * **Expiry** — ``expires_at`` is wall-clock (UTC). The router
      treats ``expires_at <= now`` as a miss; an explicit sweep job
      (REST ``POST /api/wiki/refresh``) deletes expired rows in batch.

    No vector column for we use substring + canonical-form
    matching (mirrors ``MemoryStore``'s naive-but-good-enough
    approach). When skill bodies grow past a few hundred entries,
    swap the lookup implementation in ``WikiStore.lookup`` without
    touching this schema or any caller.
    """

    __tablename__ = "wiki_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # The skill_id this entry belongs to. We key by skill rather
    # than by some abstract "kind" so a skill rename / removal can
    # invalidate its rows in one query, and so multiple skills
    # caching similar-looking queries (e.g. "travel-guide" vs a
    # future "travel-budget") never collide.
    skill_id: Mapped[str] = mapped_column(String(128), index=True)
    # The post-normalizer canonical key. Indexed because lookups go
    # through this column. We also keep raw_query for human-facing
    # /api/wiki listing — operators want to see what users typed.
    normalized_query: Mapped[str] = mapped_column(String(512), index=True)
    raw_query: Mapped[str] = mapped_column(Text, default="")
    answer: Mapped[str] = mapped_column(Text, default="")
    # ISO-formatted JSON for misc per-entry data (LLM model used,
    # tokens, source skill version). Kept as TEXT/JSON to avoid
    # adding a new column every time we record one more thing.
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    last_hit_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True,
    )
    hit_count: Mapped[int] = mapped_column(Integer, default=0)
    # Wall-clock expiry in UTC. ``None`` means "never expires" — used
    # for stable knowledge that won't change (e.g. "什么是 Python").
    # Travel-guide rows default to 30-day expiry (seasonal data).
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, index=True,
    )
    # ── Wiki v2 fields (borrowed from nvk/llm-wiki frontmatter) ──
    # ``confidence`` is a 0.0-1.0 score that goes UP when an entry is
    # re-hit / re-confirmed and DOWN with the forgetting curve. The
    # router can use it to break ties when two normalized_queries
    # match (prefer the higher-confidence row), and an operator REST
    # surface can list "low confidence" rows for review. Default 0.5
    # so legacy rows land in the middle of the range — not so high
    # that we trust them blindly, not so low that lookups skip them.
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    # JSON array of source identifiers for *this* entry.
    # Examples:
    #   ["llm:deepseek-v4-pro:turn:abc123"] — generated by main LLM
    #   ["wiki_entry:42", "wiki_entry:43"] — atomic fact crystallized
    #     out of two prior LLM answers (provenance for forgetting /
    #     supersession decisions).
    #   ["user-correction:msg-xyz"] — corrected interactively by the
    #     user, treated as ground truth.
    # The router does not parse this; it's for audit + debugging
    # + future graph traversal.
    sources_json: Mapped[str] = mapped_column(Text, default="[]")
    # JSON array of alternate normalized queries the LLM produced
    # when it crystallized this fact. The lookup() path tries the
    # canonical normalized_query FIRST, then falls back to checking
    # if the user's normalized_query appears in any other row's
    # ``aliases_json`` — this catches paraphrases that the
    # normalizer's character-level rules don't fold together yet.
    aliases_json: Mapped[str] = mapped_column(Text, default="[]")
    # Pointer to a newer entry that supersedes this one. NULL means
    # "this row is current". Set when crystallization detects a fact
    # that contradicts an existing fact: the OLD row gets
    # superseded_by = <new id> (we keep it for history) and the
    # lookup path skips superseded rows by default. The
    # ``include_superseded=True`` flag on lookup() exists for the
    # operator REST surface only.
    superseded_by: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True, index=True,
    )
    # last_confirmed_at differs from last_hit_at — last_hit_at is
    # bumped on every cache hit (read), last_confirmed_at is bumped
    # only when a NEW LLM run produces matching content, i.e. when
    # we have fresh evidence that the row is still correct. Initial
    # value mirrors created_at so a brand-new entry starts at
    # max-confidence freshness; a sweep job ages it down with the
    # Ebbinghaus curve.
    last_confirmed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True,
    )
    # ``crystal_kind`` distinguishes the row's role:
    #   "answer"      — full LLM answer cached as-is (legacy default)
    #   "atomic_fact" — short single-claim row crystallized out of an
    #                   answer (e.g. "京沪高铁约 4.5 小时"). atomic_fact
    #                   rows have higher hit potential because they
    #                   match short factual queries.
    # Default "answer" so legacy rows keep their semantics on
    # migration.
    crystal_kind: Mapped[str] = mapped_column(
        String(32), default="answer", index=True,
    )
    # ── Geo path tagging ────────────────────────────────────
    # Semicolon-separated list of geo entity references this entry is
    # about. Each token is ``<type>:<name>`` so a multi-locus fact
    # (e.g. "京沪高铁") shows both endpoints:
    #
    #   "city:北京;city:上海"
    #   "province:浙江"
    #   "district:朝阳区"
    #
    # Empty string ``""`` means "not geo-tagged" — most non-travel
    # skills will leave this blank. The lookup path uses ``LIKE
    # '%city:上海%'`` to filter; when (skill, normalized_query, geo)
    # are all asked together this lets us pick the best match for
    # the user's specific city without re-running the LLM.
    #
    # Why semicolon-separated and not a relation table:  ~5K-50K
    # row scale, LIKE on indexed column is plenty fast, and a
    # one-column migration costs nothing. We can promote to
    # ``wiki_entry_geo_links`` later if the data outgrows this.
    geo_path: Mapped[str] = mapped_column(Text, default="", index=True)


class GeoEntity(Base):
    """Hierarchical administrative-division ontology.

    A node in the China admin-division tree:

        country:中国 (level 0)
            ↓
        province:北京市 (level 1)
            ↓
        city:北京市 / city:上海市 (level 2 — direct-administered municipalities
                                   collapse province=city, but we still
                                   write both rows so children always
                                   point at a level-2 parent)
            ↓
        district:朝阳区 (level 3)

    Source data lives in ``data/geo/*.json`` (git-tracked, human-
    readable) and is ETL'd into this table at startup by
    :class:`backend.wiki.geo_store.GeoStore`. The JSON files are the
    source of truth; this table is a query-optimised mirror.

    The ``code`` column carries the National Bureau of Statistics
    administrative-division code (GB/T 2260) when known — six digits
    like ``110000`` for 北京市, ``110105`` for 朝阳区. It's optional
    because some skill-defined extensions (e.g. tourist sub-regions
    like "外滩") have no official code, in which case we synthesise
    one as ``999`` + parent_id-derived suffix.

    ``aliases_json`` lets the normalizer's geo-NER step match
    short forms — "京" → 北京市, "申" → 上海市, "鹏城" → 深圳市.
    Each entry carries its own paraphrases independently of the wiki
    row that mentions them.

    Hierarchy is via a self-referential ``parent_id``. A non-NULL
    parent is enforced for every level except level=0 (the country
    root), but we don't add a CHECK constraint — SQLite's enforcement
    is fragile and we'd rather catch bad seed data via the loader.
    """

    __tablename__ = "geo_entities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # GB/T 2260 admin-division code (when known). Indexed because
    # external systems (12306, 高德地图) refer to entities by code.
    # Unique-but-nullable: SQLite allows multiple NULLs in a unique
    # column, which is what we want.
    code: Mapped[Optional[str]] = mapped_column(
        String(20), nullable=True, unique=True, index=True,
    )
    # Canonical human-readable name. "北京市", "朝阳区". Indexed
    # because most lookups go by name.
    name: Mapped[str] = mapped_column(String(64), index=True)
    # Short form for fuzzy matching: "北京", "朝阳". Often differs
    # from name only by a trailing 市 / 省 / 区 suffix, but storing
    # explicitly avoids re-deriving it on every lookup.
    short_name: Mapped[str] = mapped_column(String(64), index=True, default="")
    # One of "country" | "province" | "city" | "district". Keeping
    # this as an explicit column rather than deriving from level
    # makes filter queries (``WHERE type='city'``) easy and self-
    # documenting.
    type: Mapped[str] = mapped_column(String(16), index=True)
    # 0=country, 1=province, 2=city, 3=district. Redundant with
    # ``type`` but enables ``ORDER BY level`` and depth-bounded tree
    # walks without a string-to-int translation.
    level: Mapped[int] = mapped_column(Integer, default=0)
    # Self-referential parent. NULL only for the country root.
    parent_id: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True, index=True,
    )
    # JSON array of alternate names: ["京", "首都"] for 北京市.
    # Used by the geo-NER pass when a wiki crystallization sees
    # "京沪高铁" and we want to recover ["北京市", "上海市"].
    aliases_json: Mapped[str] = mapped_column(Text, default="[]")
    # Optional centroid (decimal degrees, WGS-84). Filled when
    # available from DataV / OSM seed data; left NULL otherwise.
    # The wiki agent doesn't currently use these — kept so future
    # "周边 X 公里" features can land without a schema change.
    latitude: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    longitude: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


def apply_lightweight_migrations(engine) -> None:
    """Best-effort additive migrations for SQLite.

    SQLAlchemy's ``create_all`` only creates **missing tables**, not new
    columns on existing tables. We don't want to bring in alembic for
    one-off field additions, so we do the simplest thing that works for
    our SQLite-only deployments: introspect the current schema and ALTER
    TABLE the columns we know about.

    This is idempotent — every column add is wrapped in an existence
    check, so running on an already-migrated DB is a no-op.

    Add new entries to ``ADDITIVE_COLUMNS`` whenever a future version
    introduces a nullable / defaulted column on an existing table.
    """
    from sqlalchemy import inspect, text

    # (table, column, sql_fragment_for_ALTER_TABLE_ADD_COLUMN)
    ADDITIVE_COLUMNS: list[tuple[str, str, str]] = [
        # v0.10: pre_script support on cron_jobs
        ("cron_jobs", "pre_script_path", "pre_script_path TEXT"),
        ("cron_jobs", "pre_script_timeout_seconds",
         "pre_script_timeout_seconds INTEGER NOT NULL DEFAULT 30"),
        ("cron_jobs", "run_once", "run_once BOOLEAN NOT NULL DEFAULT 0"),
        ("cron_jobs", "lead_minutes", "lead_minutes INTEGER NOT NULL DEFAULT 2"),
        # ── Wiki v2 columns on wiki_entries.  All are
        # nullable / defaulted so legacy rows on a pre-v0.39 DB land
        # at sensible defaults (confidence=0.5 = "neither trusted nor
        # rejected", crystal_kind="answer" = legacy semantics, no
        # supersession, sources/aliases empty).
        ("wiki_entries", "confidence",
         "confidence REAL NOT NULL DEFAULT 0.5"),
        ("wiki_entries", "sources_json",
         "sources_json TEXT NOT NULL DEFAULT '[]'"),
        ("wiki_entries", "aliases_json",
         "aliases_json TEXT NOT NULL DEFAULT '[]'"),
        ("wiki_entries", "superseded_by",
         "superseded_by INTEGER"),
        ("wiki_entries", "last_confirmed_at",
         "last_confirmed_at DATETIME"),
        ("wiki_entries", "crystal_kind",
         "crystal_kind VARCHAR(32) NOT NULL DEFAULT 'answer'"),
        # ── Graph / knowledge base isolation.
        ("user_memories", "knowledge_base_id",
         "knowledge_base_id VARCHAR(64) NOT NULL DEFAULT 'default'"),
        # ── Geo path tagging.  Empty default so existing
        # rows on a pre-v0.39.1 DB are correctly marked as
        # "untagged" rather than NULL.
        ("wiki_entries", "geo_path",
         "geo_path TEXT NOT NULL DEFAULT ''"),
        # v1.1.0 — per-tool permission overrides for attached MCP
        # servers. Default '{}' for pre-v1.1.0 rows so the existing
        # confirm-everywhere semantics are preserved until the
        # operator runs ``mcp_manage(action='promote', ...)``.
        ("mcp_servers", "tool_override_permission_json",
         "tool_override_permission_json TEXT NOT NULL DEFAULT '{}'"),
    ]

    inspector = inspect(engine)
    with engine.begin() as conn:
        for table, column, ddl in ADDITIVE_COLUMNS:
            if not inspector.has_table(table):
                continue  # create_all will handle it on a fresh DB
            existing = {col["name"] for col in inspector.get_columns(table)}
            if column in existing:
                continue
            conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {ddl}'))
