"""Filesystem-backed skill loader.

Skills live in their own directory under ``workspace/skills`` and can use
either of two on-disk formats. Both coexist; the loader auto-detects.

**Hermes format** (preferred from v0.7 onward; matches agentskills.io):

    workspace/skills/<skill_id>/
        SKILL.md            single file: YAML frontmatter (--- delimited) +
                            markdown body that becomes the prompt content
        references/         optional knowledge snapshots
        templates/          optional starter files
        scripts/            optional re-runnable scripts

**Legacy format** (v0.1 layout, still supported for example-ping):

    workspace/skills/<skill_id>/
        skill.yaml          metadata only (name, description, tags, ...)
        instructions.md     the prompt body the agent reads
        resources/          optional resources

When both ``SKILL.md`` and ``skill.yaml`` exist in the same directory,
``SKILL.md`` wins — it's the unified format we'd rather move toward.

The loader only parses metadata up front; the body is read lazily via
:meth:`SkillLoader.read_body` so we don't pay the I/O cost on every list.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING

import yaml
from loguru import logger

if TYPE_CHECKING:
    # Forward reference only — keeps this module importable without
    # the v0.11 usage subsystem (e.g. very old smoke harnesses).
    from .usage import UsageStore

# Match the YAML frontmatter at the very start of a SKILL.md file.
# Group 1 is the YAML body; group 2 is the rest of the file.
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


@dataclass(slots=True)
class SkillStreamingSection:
    """One segment of a streaming-enabled skill.

    ``prompt`` is the section-specific constraint injected into the
    system prompt so the LLM knows to generate ONLY this piece.
    ``title`` is prepended to the dispatched IM message so the user
    sees a labelled block (e.g. "🍜 当地美食").
    """

    id: str
    title: str
    prompt: str


@dataclass(slots=True)
class SkillManifest:
    """In-memory representation of a skill loaded from disk.

    ``body_path`` points at whichever file holds the markdown the agent
    will see — ``SKILL.md`` for Hermes-format skills, ``instructions.md``
    for the legacy layout.

    ``triggers`` is the list of substring phrases the IM-side
    skill router (``backend/agent/routing/skill_match.py``) matches against
    incoming user messages. Sourced from ``metadata.zlagent.triggers``
    in the YAML frontmatter (Hermes format). Empty list means the
    skill is reference-only / cron-only and never auto-injects on IM.

    ``wiki_cache_enabled`` + ``wiki_cache_ttl_seconds`` opt
    the skill into the answer cache. When enabled, run_turn checks
    the wiki BEFORE doing any LLM work; on miss it runs the normal
    agent loop and asynchronously writes the answer back. Sourced
    from ``metadata.zlagent.wiki_cache.{enabled, ttl_seconds}``.
    ``ttl_seconds=None`` means timeless (used for stable knowledge).
    Defaults to disabled — opt-in keeps non-query skills off the
    cache path.

    ``rich_output`` opts the skill into structured
    :class:`~backend.rich.schema.RichMessage` replies. When enabled
    the agent's skill-specific system prompt appends a rich-content
    fence protocol description; the LLM emits a ``rich-content`` JSON
    fence inside its reply, the loop parses it out, and the gateway
    renders it via markdown cards (wecom_bot) or aligned text (weixin).
    Token-level streaming is automatically *suppressed* for
    rich_output skills because the user would otherwise see raw JSON
    mid-stream; the whole answer gets delivered as one rich message
    instead. Sourced from ``metadata.zlagent.rich_output`` in the
    Hermes frontmatter or ``rich_output`` in the legacy ``skill.yaml``.

    ``crystallize`` opts the skill into the post-answer
    crystallization pipeline (borrowed from nvk/llm-wiki's
    "compounding from exploration" + LLM Wiki v2's atomic-fact
    extraction). When enabled, after a successful answer is written
    to the wiki, a small follow-up LLM call extracts 3-5 atomic
    facts from that answer (e.g. "京沪高铁约 4.5 小时", "二等座
    553-650 元") and stores each as its own
    ``crystal_kind="atomic_fact"`` row. Future short factual
    queries hit those atomic rows directly without re-running the
    full LLM. Defaults to ``False`` so the costly second LLM call
    is opt-in per skill — typically enabled for query-type skills
    (travel-guide, weather-now, news-digest) and skipped for
    procedural skills (skill-authoring, mcp-discovery).
    """

    id: str
    name: str
    description: str = ""
    created_by: str = "user"  # user | agent | harness
    tags: list[str] = field(default_factory=list)
    version: str = "0.1.0"
    body_path: Optional[Path] = None
    root: Optional[Path] = None
    format: str = "legacy"  # "hermes" (SKILL.md) | "legacy" (skill.yaml + instructions.md)
    triggers: list[str] = field(default_factory=list)
    wiki_cache_enabled: bool = False
    wiki_cache_ttl_seconds: Optional[int] = None
    streaming_sections: list[SkillStreamingSection] = field(default_factory=list)
    streaming_parallel: int = 1
    rich_output: bool = False
    crystallize: bool = False


class SkillLoader:
    """Scan ``workspace/skills`` and produce manifests.

    Optionally hosts a :class:`UsageStore` (v0.11) so every body load
    can bump the skill's view counter. Pass ``usage_store=None`` (the
    default) for environments where telemetry is not desired (legacy
    smoke tests, importer scripts).
    """

    def __init__(
        self,
        skills_dir: Path,
        *,
        usage_store: Optional["UsageStore"] = None,
    ) -> None:
        self._skills_dir = skills_dir
        self._skills: dict[str, SkillManifest] = {}
        self._usage_store = usage_store

    def load(self) -> dict[str, SkillManifest]:
        self._skills.clear()
        if not self._skills_dir.exists():
            logger.info("skills directory missing: {}", self._skills_dir)
            return {}
        # Recurse one level so Hermes-style category directories (e.g.
        # ``skills/research/arxiv/SKILL.md``) get picked up. We deliberately
        # cap recursion depth at 3 to avoid runaway crawls.
        for child in sorted(self._skills_dir.iterdir()):
            if child.is_dir():
                self._scan(child, depth=1)
        logger.info("loaded {} skill(s) from {}", len(self._skills), self._skills_dir)
        return dict(self._skills)

    def list(self) -> list[SkillManifest]:
        return list(self._skills.values())

    def get(self, skill_id: str) -> Optional[SkillManifest]:
        return self._skills.get(skill_id)

    def read_body(self, skill_id: str) -> Optional[str]:
        """Return the markdown body for ``skill_id`` or ``None`` if missing.

        For Hermes-format skills the frontmatter is stripped; for legacy
        skills the entire ``instructions.md`` is returned verbatim.
        """
        manifest = self._skills.get(skill_id)
        if manifest is None or manifest.body_path is None:
            return None
        try:
            raw = manifest.body_path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("failed to read body for skill {!r}: {}", skill_id, exc)
            return None
        if manifest.format == "hermes":
            match = _FRONTMATTER_RE.match(raw)
            if match is not None:
                body = match.group(2).strip()
            else:
                body = raw.strip()
        else:
            body = raw.strip()
        # v0.11: best-effort view bump. Failures here MUST NOT break the
        # body load — see UsageStore docstring.
        if self._usage_store is not None:
            try:
                self._usage_store.record_view(skill_id)
            except Exception as exc:  # noqa: BLE001
                logger.debug("usage record_view failed for {}: {}", skill_id, exc)
        return body

    # Internal -----------------------------------------------------------
    def _scan(self, folder: Path, *, depth: int) -> None:
        manifest = self._load_one(folder)
        if manifest is not None:
            if manifest.id in self._skills:
                logger.warning(
                    "duplicate skill id {!r} at {} (previous: {}); keeping first",
                    manifest.id, folder, self._skills[manifest.id].root,
                )
            else:
                self._skills[manifest.id] = manifest
            return
        if depth >= 3:
            return
        for child in sorted(folder.iterdir()):
            if child.is_dir():
                self._scan(child, depth=depth + 1)

    def _load_one(self, folder: Path) -> Optional[SkillManifest]:
        # Prefer Hermes format when available.
        skill_md = folder / "SKILL.md"
        if skill_md.exists() and skill_md.is_file():
            return self._load_hermes(skill_md, folder)
        legacy_yaml = folder / "skill.yaml"
        if legacy_yaml.exists() and legacy_yaml.is_file():
            return self._load_legacy(legacy_yaml, folder)
        return None

    def _load_hermes(self, skill_md: Path, folder: Path) -> Optional[SkillManifest]:
        try:
            raw = skill_md.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("failed to read {}: {}", skill_md, exc)
            return None
        match = _FRONTMATTER_RE.match(raw)
        meta: dict[str, Any] = {}
        if match is not None:
            try:
                parsed = yaml.safe_load(match.group(1)) or {}
                if isinstance(parsed, dict):
                    meta = parsed
            except yaml.YAMLError as exc:
                logger.warning(
                    "skipping {}: invalid frontmatter: {}", skill_md, exc,
                )
                return None
        # Hermes nests our extension fields under metadata.hermes.*
        hermes_meta = {}
        try:
            hermes_meta = (meta.get("metadata") or {}).get("hermes") or {}
            if not isinstance(hermes_meta, dict):
                hermes_meta = {}
        except AttributeError:
            hermes_meta = {}
        # ZLAgent extension fields (IM trigger phrases) live
        # under metadata.zlagent.* and don't conflict with Hermes' own.
        # We deliberately accept tags from BOTH buckets so a skill that
        # was authored upstream (Hermes-only metadata) and later annotated
        # for ZLAgent routing keeps its provenance.
        zlagent_meta: dict[str, Any] = {}
        try:
            zlagent_meta = (meta.get("metadata") or {}).get("zlagent") or {}
            if not isinstance(zlagent_meta, dict):
                zlagent_meta = {}
        except AttributeError:
            zlagent_meta = {}
        triggers = [
            str(t).strip()
            for t in (zlagent_meta.get("triggers") or [])
            if t and isinstance(t, (str, int))
        ]
        # wiki cache opt-in. Lives under metadata.zlagent.wiki_cache:
        #   enabled: true | false   (default: false)
        #   ttl_seconds: int | null (default: null = never expire)
        # We accept ``ttl_days`` as a convenience alias because authors
        # think in days for travel guides; we convert to seconds here so
        # downstream code (WikiStore.add) sees one canonical unit.
        wiki_cfg = zlagent_meta.get("wiki_cache") or {}
        if not isinstance(wiki_cfg, dict):
            wiki_cfg = {}
        wiki_enabled = bool(wiki_cfg.get("enabled", False))
        wiki_ttl: Optional[int]
        if "ttl_seconds" in wiki_cfg and wiki_cfg["ttl_seconds"] is not None:
            try:
                wiki_ttl = int(wiki_cfg["ttl_seconds"])
            except (TypeError, ValueError):
                logger.warning(
                    "skill {!r}: invalid ttl_seconds={!r}; treating as unset",
                    folder.name, wiki_cfg["ttl_seconds"],
                )
                wiki_ttl = None
        elif "ttl_days" in wiki_cfg and wiki_cfg["ttl_days"] is not None:
            try:
                wiki_ttl = int(wiki_cfg["ttl_days"]) * 86_400
            except (TypeError, ValueError):
                logger.warning(
                    "skill {!r}: invalid ttl_days={!r}; treating as unset",
                    folder.name, wiki_cfg["ttl_days"],
                )
                wiki_ttl = None
        else:
            wiki_ttl = None
        if wiki_ttl is not None and wiki_ttl <= 0:
            # Negative / zero TTL = "never cache" — disable rather
            # than write rows that expire immediately.
            wiki_enabled = False
            wiki_ttl = None
        # streaming sections. Opt-in under metadata.zlagent.streaming:
        #   parallel: int           (default 1 — sequential)
        #   sections:               (list of {id, title, prompt})
        streaming_cfg = zlagent_meta.get("streaming") or {}
        if not isinstance(streaming_cfg, dict):
            streaming_cfg = {}
        try:
            streaming_parallel = max(1, int(streaming_cfg.get("parallel") or 1))
        except (TypeError, ValueError):
            streaming_parallel = 1
        raw_sections = streaming_cfg.get("sections") or []
        streaming_sections: list[SkillStreamingSection] = []
        if isinstance(raw_sections, list):
            for _sec in raw_sections:
                if not isinstance(_sec, dict):
                    continue
                _sid = str(_sec.get("id") or "").strip()
                _title = str(_sec.get("title") or _sid).strip()
                _prompt = str(_sec.get("prompt") or "").strip()
                if _sid and _prompt:
                    streaming_sections.append(SkillStreamingSection(
                        id=_sid,
                        title=_title,
                        prompt=_prompt,
                    ))
        # rich output opt-in. Accepted under
        # ``metadata.zlagent.rich_output`` (boolean). Any truthy YAML
        # value is converted via Python truthiness.
        rich_output = bool(zlagent_meta.get("rich_output", False))
        # crystallize opt-in (atomic fact extraction post-
        # answer). Accepted under ``metadata.zlagent.crystallize``;
        # any truthy value enables. See the SkillManifest docstring
        # for the full lifecycle.
        crystallize = bool(zlagent_meta.get("crystallize", False))
        return SkillManifest(
            id=str(meta.get("name") or folder.name),
            name=str(meta.get("name") or folder.name),
            description=str(meta.get("description") or ""),
            created_by=str(hermes_meta.get("created_by") or "user"),
            tags=[str(t) for t in (hermes_meta.get("tags") or []) if t],
            version=str(meta.get("version") or "1.0.0"),
            body_path=skill_md,
            root=folder,
            format="hermes",
            triggers=[t for t in triggers if t],
            wiki_cache_enabled=wiki_enabled,
            wiki_cache_ttl_seconds=wiki_ttl,
            streaming_sections=streaming_sections,
            streaming_parallel=streaming_parallel,
            rich_output=rich_output,
            crystallize=crystallize,
        )

    def _load_legacy(self, manifest_path: Path, folder: Path) -> Optional[SkillManifest]:
        try:
            data = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("failed to parse {}: {}", manifest_path, exc)
            return None
        if not isinstance(data, dict):
            logger.warning("skill.yaml at {} is not a mapping; skipping", manifest_path)
            return None
        body_path = folder / data.get("instructions", "instructions.md")
        if not body_path.exists():
            body_path = None  # type: ignore[assignment]
        return SkillManifest(
            id=str(data.get("id") or folder.name),
            name=str(data.get("name") or folder.name),
            description=str(data.get("description") or ""),
            created_by=str(data.get("created_by") or "user"),
            tags=[str(t) for t in (data.get("tags") or []) if t],
            version=str(data.get("version") or "0.1.0"),
            body_path=body_path,
            root=folder,
            format="legacy",
            # legacy skill.yaml can opt into rich_output at
            # the top level, mirroring the Hermes frontmatter knob.
            rich_output=bool(data.get("rich_output", False)),
            # ditto for crystallize.
            crystallize=bool(data.get("crystallize", False)),
        )
