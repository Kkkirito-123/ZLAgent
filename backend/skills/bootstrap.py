"""Skill subsystem boot helper.

Extracted in v0.40 from :func:`backend.app.lifespan` so the FastAPI
startup orchestration reads top-down. Bundles every component that
the skill pipeline needs at runtime:

* :class:`UsageStore` — single sidecar JSON shared by SkillLoader,
  read_file, and skill_manage.
* :class:`SkillHistoryStore` — append-only mutation log
  (one record per create/edit/patch).
* :class:`SkillGuard` — static security scanner that gates every
  skill_manage write.
* :class:`SkillLoader` — manifest reader + body cache.
* :class:`WikiStore` — answer cache, optionally Redis-backed.
* :class:`GeoStore` — geo ontology, seeded from bundled JSON.

Returns a dataclass-style namespace so the caller doesn't have to
unpack a 6-tuple at the call site.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from loguru import logger

from .guard import SkillGuard
from .history import SkillHistoryStore
from .loader import SkillLoader
from .usage import UsageStore
from ..wiki.geo_store import GeoStore
from ..wiki.store import WikiStore


@dataclass(slots=True)
class SkillSubsystem:
    usage_store: UsageStore
    skill_history_store: SkillHistoryStore
    skill_guard: SkillGuard
    skill_loader: SkillLoader
    wiki_store: WikiStore
    geo_store: GeoStore


def build_skill_subsystem(
    settings: Any,
    *,
    redis_backend: Optional[Any],
    session_factory: Any,
) -> SkillSubsystem:
    """Boot the v0.11 / v0.15 / v0.20 / v0.37 / v0.39.1 skill stack.

    All errors here are non-fatal: the geo seed in particular is
    advisory at boot — a missing seed just means geo-narrowed wiki
    lookups degrade back to v0.39 global lookup behaviour.
    """
    skills_dir = settings.workspace_dir / "skills"

    usage_store = UsageStore(skills_dir)
    skill_history_store = SkillHistoryStore(skills_dir)

    skill_guard = SkillGuard(
        enabled=settings.skill_guard_enabled,
        strict_for_agent=settings.skill_guard_strict_for_agent,
    )

    skill_loader = SkillLoader(
        skills_dir,
        usage_store=usage_store,
    )
    skill_loader.load()

    # answer cache (the "wiki"). Shared across users so a
    # travel guide written for user A can serve user B in <100ms.
    # Construction is unconditional because the table already exists
    # from create_all(); passing wiki_store=None to AgentLoop is the
    # way to disable the layer at runtime.
    wiki_store = WikiStore(
        redis_backend=redis_backend,
        redis_ttl_seconds=settings.redis_wiki_ttl_seconds,
    )

    # Geographic ontology. ETL the bundled JSON seeds into
    # the ``geo_entities`` SQLite table at boot. Failures here are
    # non-fatal: a missing geo store just means geo_filter lookups
    # return all matches (degrade back to v0.39 behaviour). The seed
    # is upsert-by-code so re-running on every boot is idempotent.
    geo_store = GeoStore(session_factory=session_factory)
    try:
        geo_report = geo_store.seed_from_json()
        logger.info("[geo] startup seed: {}", geo_report)
    except Exception as exc:  # noqa: BLE001 — advisory at boot
        logger.warning("[geo] startup seed failed (non-fatal): {}", exc)

    logger.info(
        "wiki store ready (skill-opt-in via metadata.zlagent.wiki_cache,"
        " redis={})",
        "on" if redis_backend is not None else "off",
    )
    logger.info(
        "skill loader ready: {} skill(s) under {}",
        len(skill_loader.list()),
        skills_dir,
    )

    return SkillSubsystem(
        usage_store=usage_store,
        skill_history_store=skill_history_store,
        skill_guard=skill_guard,
        skill_loader=skill_loader,
        wiki_store=wiki_store,
        geo_store=geo_store,
    )
