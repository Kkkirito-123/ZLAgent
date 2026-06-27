"""SkillCurator — autonomous skill consolidation (v0.11).

The curator is the v0.9 review fork's *long-horizon* counterpart. The review
fork looks at one turn at a time and decides "should I patch / create a
skill right now?". The curator zooms out: it scans **all** agent-created
skills periodically and decides which have gone stale, which look like
duplicates, and which the user has explicitly pinned (and must therefore
be left alone).

Strict invariants — these are load-bearing security properties:

1. **Only ``created_by="agent"`` skills are touched.** User-authored or
   bundled skills are *off-limits* regardless of how stale they look.
   This is enforced by :func:`UsageStore.is_curator_managed`.

2. **Pinned skills bypass everything.** If ``pinned=True`` the curator
   reports the skill but never proposes a state change.

3. **Never deletes — only archives.** ``archive`` flips state to
   ``archived`` and writes ``archived_at``; the on-disk SKILL.md and
   support files stay where they are. Reversible by setting state back
   to ``active``.

4. **Dedupe never auto-merges.** ``dedupe_review`` produces a list of
   candidate pairs (``merge_from`` / ``merge_into``) with similarity
   scores; an explicit user-confirmed call to ``archive`` with
   ``absorbed_into=<survivor>`` is what actually executes a merge.

The curator itself is **read-only** w.r.t. SKILL.md content. It only
mutates the sidecar (state / pinned / archived_at / absorbed_into) via
the UsageStore. Any change to a skill's body must go through the
``skill_manage`` tool with proper provenance.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from .loader import SkillLoader, SkillManifest
from .usage import (
    STATE_ACTIVE,
    STATE_ARCHIVED,
    STATE_STALE,
    UsageStore,
    is_curator_managed,
    latest_activity_at,
    activity_count,
)

# Defaults that match Hermes' conservative tuning. Override per-instance
# via the constructor or via Settings on the wiring path.
DEFAULT_STALE_AFTER_DAYS = 30
DEFAULT_ARCHIVE_AFTER_DAYS = 90
DEFAULT_DEDUPE_THRESHOLD = 0.70


@dataclass(slots=True)
class SkillStatus:
    """Per-skill lifecycle snapshot returned by :meth:`SkillCurator.review`.

    ``eligibility`` summarises the curator's read-only verdict:

    * ``"untracked"`` — manifest on disk but no sidecar entry yet
    * ``"off_limits"`` — sidecar present but ``created_by`` is not "agent"
    * ``"pinned_skip"`` — agent-created but pinned by the user
    * ``"healthy"`` — used recently enough; no action proposed
    * ``"stale_candidate"`` — exceeds ``stale_after_days`` since last activity
    * ``"archive_candidate"`` — exceeds ``archive_after_days`` since last activity
    * ``"already_archived"`` — already in archived state
    """

    name: str
    state: str
    pinned: bool
    created_by: Optional[str]
    days_since_activity: Optional[float]
    activity_count: int
    eligibility: str
    last_activity_at: Optional[str]


@dataclass(slots=True)
class DuplicateCandidate:
    """One ``(survivor, victim)`` pair from :meth:`SkillCurator.dedupe_review`."""

    merge_from: str   # skill that would be archived
    merge_into: str   # skill that would survive
    similarity: float
    name_score: float
    description_score: float
    body_score: float
    blockers: List[str] = field(default_factory=list)


@dataclass(slots=True)
class CuratorReport:
    """High-level summary returned by :meth:`SkillCurator.run_review`."""

    total_skills: int
    agent_created_count: int
    healthy: int
    stale_candidates: List[str]
    archive_candidates: List[str]
    pinned_skipped: List[str]
    untracked: List[str]
    duplicate_candidates: List[DuplicateCandidate]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _days_since(timestamp: Optional[str]) -> Optional[float]:
    if not timestamp:
        return None
    try:
        ts = datetime.fromisoformat(timestamp)
    except (TypeError, ValueError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return max(0.0, (_utcnow() - ts).total_seconds() / 86400.0)


class SkillCurator:
    """Periodic skill-collection caretaker.

    The class is intentionally stateless: every public method reads the
    current sidecar fresh, computes its result, and either returns a
    report (read-only) or hands back to UsageStore for a single mutation
    (pin/archive/etc.). This keeps the curator safe to instantiate
    multiple times in the same process and immune to drift between an
    in-memory cache and the on-disk truth.
    """

    def __init__(
        self,
        *,
        loader: SkillLoader,
        usage_store: UsageStore,
        stale_after_days: int = DEFAULT_STALE_AFTER_DAYS,
        archive_after_days: int = DEFAULT_ARCHIVE_AFTER_DAYS,
        dedupe_threshold: float = DEFAULT_DEDUPE_THRESHOLD,
    ) -> None:
        if archive_after_days <= stale_after_days:
            raise ValueError(
                "archive_after_days must be greater than stale_after_days"
            )
        if not 0.0 < dedupe_threshold <= 1.0:
            raise ValueError("dedupe_threshold must be in (0, 1]")
        self._loader = loader
        self._usage = usage_store
        self.stale_after_days = stale_after_days
        self.archive_after_days = archive_after_days
        self.dedupe_threshold = dedupe_threshold

    # =================================================================
    # Read-only review surface
    # =================================================================

    def review(self) -> List[SkillStatus]:
        """Return a per-skill status snapshot.

        Reloads the loader so newly-created skills (e.g. from the latest
        review fork) are visible. Failures inside one skill's evaluation
        never abort the whole report — the row's ``eligibility`` simply
        becomes ``"error"``.
        """
        self._loader.load()
        manifests = self._loader.list()
        usage = self._usage.all()

        out: List[SkillStatus] = []
        for manifest in manifests:
            try:
                out.append(self._evaluate(manifest, usage.get(manifest.id)))
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[curator] failed to evaluate skill {}: {}", manifest.id, exc,
                )
                out.append(
                    SkillStatus(
                        name=manifest.id,
                        state="unknown",
                        pinned=False,
                        created_by=None,
                        days_since_activity=None,
                        activity_count=0,
                        eligibility="error",
                        last_activity_at=None,
                    )
                )
        return out

    def run_review(self) -> CuratorReport:
        """Run :meth:`review` and aggregate into a high-level report.

        Pure observation — does not mutate state. The orchestrating
        :class:`backend.skills.curator_service.SkillCuratorService`
        calls this on a timer and logs the result.
        """
        statuses = self.review()
        agent_created = [s for s in statuses if s.created_by == "agent"]
        report = CuratorReport(
            total_skills=len(statuses),
            agent_created_count=len(agent_created),
            healthy=sum(1 for s in agent_created if s.eligibility == "healthy"),
            stale_candidates=[
                s.name for s in agent_created if s.eligibility == "stale_candidate"
            ],
            archive_candidates=[
                s.name for s in agent_created if s.eligibility == "archive_candidate"
            ],
            pinned_skipped=[
                s.name for s in agent_created if s.eligibility == "pinned_skip"
            ],
            untracked=[s.name for s in statuses if s.eligibility == "untracked"],
            duplicate_candidates=self.dedupe_review(),
        )
        return report

    def dedupe_review(self) -> List[DuplicateCandidate]:
        """Find ``(merge_from, merge_into)`` pairs above the threshold.

        Similarity is a weighted blend of:
          * skill name (40%): renames within the same conceptual cluster
            still register, e.g. ``arxiv-daily`` vs ``arxiv-digest``
          * description (30%): Hermes-format frontmatter description
          * body (30%): SKILL.md after frontmatter strip

        The pair direction is deterministic: the **older** skill (by
        ``created_at``) wins as ``merge_into`` so consolidation always
        flows toward the established skill rather than oscillating.
        """
        self._loader.load()
        manifests = [m for m in self._loader.list()]
        # Limit to agent-created so we don't propose merging user skills.
        usage_all = self._usage.all()
        candidates: List[Tuple[SkillManifest, Dict[str, Any], str]] = []
        for m in manifests:
            rec = usage_all.get(m.id, {})
            if not is_curator_managed(rec):
                continue
            if rec.get("state") == STATE_ARCHIVED:
                continue
            body = self._safe_body(m.id) or ""
            candidates.append((m, rec, body))

        out: List[DuplicateCandidate] = []
        seen_pairs: set[frozenset[str]] = set()
        for i in range(len(candidates)):
            mi, ri, bi = candidates[i]
            for j in range(i + 1, len(candidates)):
                mj, rj, bj = candidates[j]
                key = frozenset({mi.id, mj.id})
                if key in seen_pairs:
                    continue
                seen_pairs.add(key)
                pair = self._score_pair(mi, ri, bi, mj, rj, bj)
                if pair is not None and pair.similarity >= self.dedupe_threshold:
                    out.append(pair)

        # Highest similarity first — actionable items at the top.
        out.sort(key=lambda c: c.similarity, reverse=True)
        return out

    # =================================================================
    # Mutation surface (always single-target, always best-effort)
    # =================================================================

    def pin(self, skill_name: str, *, pinned: bool = True) -> bool:
        """Set / clear the pinned flag. Returns True on a real change."""
        rec = self._usage.get(skill_name)
        if not is_curator_managed(rec) and rec.get("created_by") is not None:
            # User skills can be pinned too — it's a no-op safety boost,
            # not a curator privilege escalation. Still allow it.
            pass
        if bool(rec.get("pinned")) == bool(pinned):
            return False
        self._usage.set_pinned(skill_name, pinned)
        return True

    def mark_stale(self, skill_name: str) -> bool:
        """Move an agent-created skill into ``stale`` state.

        Refused for non-agent-created or pinned skills. Returns True iff
        a transition actually occurred.
        """
        rec = self._usage.get(skill_name)
        if not is_curator_managed(rec):
            logger.info(
                "[curator] refusing to mark non-agent skill {!r} as stale", skill_name,
            )
            return False
        if rec.get("pinned"):
            logger.info(
                "[curator] refusing to mark pinned skill {!r} as stale", skill_name,
            )
            return False
        if rec.get("state") == STATE_STALE:
            return False
        self._usage.set_state(skill_name, STATE_STALE)
        return True

    def mark_active(self, skill_name: str) -> bool:
        """Restore a skill (any state) back to ``active``.

        Always allowed — even on user skills — because reverting the
        sidecar state can never destroy data. Useful when a curator
        decision was wrong and the user wants the skill back.
        """
        rec = self._usage.get(skill_name)
        if rec.get("state") == STATE_ACTIVE:
            return False
        self._usage.set_state(skill_name, STATE_ACTIVE)
        return True

    def archive(
        self, skill_name: str, *, absorbed_into: Optional[str] = None
    ) -> bool:
        """Soft-archive an agent-created skill. Refuses pinned + non-agent."""
        rec = self._usage.get(skill_name)
        if not is_curator_managed(rec):
            logger.info(
                "[curator] refusing to archive non-agent skill {!r}", skill_name,
            )
            return False
        if rec.get("pinned"):
            logger.info(
                "[curator] refusing to archive pinned skill {!r}", skill_name,
            )
            return False
        if rec.get("state") == STATE_ARCHIVED:
            return False
        if absorbed_into and absorbed_into == skill_name:
            return False
        self._usage.archive(skill_name, absorbed_into=absorbed_into)
        return True

    def compact(self) -> int:
        """Archive stale agent skills, keeping pinned and user skills intact."""
        count = 0
        for status in self.review():
            if status.eligibility in {"stale_candidate", "archive_candidate"}:
                if self.archive(status.name):
                    count += 1
        if count:
            logger.info("[curator] compacted {} skill(s)", count)
        return count

    # =================================================================
    # Internals
    # =================================================================

    def _evaluate(
        self, manifest: SkillManifest, record: Optional[Dict[str, Any]]
    ) -> SkillStatus:
        if not record:
            return SkillStatus(
                name=manifest.id,
                state="unknown",
                pinned=False,
                created_by=None,
                days_since_activity=None,
                activity_count=0,
                eligibility="untracked",
                last_activity_at=None,
            )

        created_by = record.get("created_by")
        pinned = bool(record.get("pinned"))
        state = str(record.get("state") or STATE_ACTIVE)
        last_activity = latest_activity_at(record)
        days = _days_since(last_activity)
        if days is None:
            # Never used; fall back to created_at as the activity anchor
            # so brand-new skills aren't immediately "stale" the moment
            # they cross the boundary without ever being read.
            days = _days_since(record.get("created_at"))
        acount = activity_count(record)

        if state == STATE_ARCHIVED:
            eligibility = "already_archived"
        elif not is_curator_managed(record):
            eligibility = "off_limits"
        elif pinned:
            eligibility = "pinned_skip"
        elif days is not None and days >= self.archive_after_days:
            eligibility = "archive_candidate"
        elif days is not None and days >= self.stale_after_days:
            eligibility = "stale_candidate"
        else:
            eligibility = "healthy"

        return SkillStatus(
            name=manifest.id,
            state=state,
            pinned=pinned,
            created_by=created_by,
            days_since_activity=days,
            activity_count=acount,
            eligibility=eligibility,
            last_activity_at=last_activity,
        )

    def _safe_body(self, skill_name: str) -> Optional[str]:
        try:
            return self._loader.read_body(skill_name)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[curator] read_body failed for {}: {}", skill_name, exc,
            )
            return None

    def _score_pair(
        self,
        mi: SkillManifest, ri: Dict[str, Any], bi: str,
        mj: SkillManifest, rj: Dict[str, Any], bj: str,
    ) -> Optional[DuplicateCandidate]:
        # Direction: the older skill (by sidecar created_at, or fallback to
        # the manifest id alphabetically) is the survivor (merge_into).
        ti = ri.get("created_at") or "9999"
        tj = rj.get("created_at") or "9999"
        if ti <= tj:
            survivor, victim = mi, mj
            survivor_body, victim_body = bi, bj
            survivor_rec, victim_rec = ri, rj
        else:
            survivor, victim = mj, mi
            survivor_body, victim_body = bj, bi
            survivor_rec, victim_rec = rj, ri

        name_score = SequenceMatcher(None, survivor.id, victim.id).ratio()
        desc_score = SequenceMatcher(
            None, survivor.description or "", victim.description or "",
        ).ratio()
        body_score = SequenceMatcher(None, survivor_body, victim_body).ratio()
        # Weighted blend.
        similarity = 0.4 * name_score + 0.3 * desc_score + 0.3 * body_score

        blockers: List[str] = []
        if survivor_rec.get("pinned"):
            blockers.append(f"survivor {survivor.id!r} is pinned")
        if victim_rec.get("pinned"):
            blockers.append(f"victim {victim.id!r} is pinned")

        return DuplicateCandidate(
            merge_from=victim.id,
            merge_into=survivor.id,
            similarity=round(similarity, 4),
            name_score=round(name_score, 4),
            description_score=round(desc_score, 4),
            body_score=round(body_score, 4),
            blockers=blockers,
        )
