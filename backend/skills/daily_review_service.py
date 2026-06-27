"""DailyReviewService — periodic end-of-day review runner (v0.43 phase B).

Counterpart to :mod:`backend.skills.consolidator_service` /
:mod:`backend.skills.curator_service`. Where those run pure-file
maintenance, this one drives the **end-of-day review fork** the user
asked for in the 钱学森三层递阶记忆体 design:

    "skill 一开始不需要控制论去梳理，而是当你完成一天的工作量的时候，
     或者完成某个阶段的时候，利用控制论去对 skill 梳理总结。"

Cadence: warmup ~ 1 h after boot, then every 24 h. The service:

1. Collects the past 24 h of L2 (``agent_note``) + L3 (``user_fact``)
   memory writes from :class:`MemoryStore`.
2. Walks :class:`SkillHistoryStore` for skill mutations in the same
   window and snapshots :class:`SkillUsageStore` for usage counters.
3. Builds a structured ``user_block`` text describing the data.
4. Calls ``agent.run_end_of_day_review(user_block)`` — that path uses
   :data:`backend.agent.prompts.END_OF_DAY_REVIEW_PROMPT` plus the
   looser :meth:`PostTurnPipeline.end_of_day_review_action_filter`,
   so the review fork can ``skill_manage(create / edit / patch)``
   memories into skills.
5. Stores the resulting summary on ``last_summary`` and exposes it
   via REST (``/api/review/state`` / ``/api/review/run``).

Design constraints, mirroring the other periodic services:

* **Never break boot** — every step is wrapped in try/except, the
  service logs and continues. A daily review failure must not knock
  out the rest of the app.
* **Operator-controlled** — disabled flag in Settings + REST kill
  switch via the operator's existing /api/* surface.
* **Synchronous data collection / async LLM call** — collection is
  pure SQLAlchemy + JSONL, fast enough to run in the asyncio loop;
  only the actual LLM-driven review needs ``await``.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, TYPE_CHECKING

from loguru import logger

from ..db.models import DeliveryTarget as DeliveryTargetRow
from ..db.session import session_scope
from ..gateways.base import DeliveryTarget, OutgoingMessage
from ..graph.source import collect_graph_records
from ..memory.store import (
    KIND_AGENT_NOTE,
    KIND_USER_FACT,
    MemoryStore,
)
from .review_action_log import ReviewActionLog

if TYPE_CHECKING:
    from ..agent.loop import AgentLoop
    from ..gateways.manager import GatewayManager
    from .history import SkillHistoryStore
    from .usage import SkillUsageStore


# --------------------------------------------------------------------- helpers

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _diff_memory_snapshot(
    before: dict[int, bool], after: dict[int, bool],
) -> tuple[list[int], list[int]]:
    """Compute ``(new_ids, archived_ids)`` from two snapshots.

    * ``new_ids``: rows present after but not before — the review
      created these via ``memory_manage(upsert/create/...)``.
    * ``archived_ids``: rows whose flag flipped ``False → True`` —
      typically created by ``memory_manage(consolidate)`` merges.

    Unchanged rows and unarchivings are ignored; the review fork is
    not expected to unarchive.
    """
    new_ids = sorted(after.keys() - before.keys())
    archived_ids = sorted(
        mid for mid, flag in after.items()
        if flag and mid in before and not before[mid]
    )
    return new_ids, archived_ids


def _parse_iso(raw: Any) -> Optional[datetime]:
    """Best-effort ISO 8601 parse — returns ``None`` on any failure.

    The memory store hands us ``created_at`` as ISO strings (already
    serialized through ``_row_to_dict``); the skill history store
    writes ``timestamp`` as ``YYYY-MM-DDTHH:MM:SSZ``. ``fromisoformat``
    in Py 3.11+ handles both, but trailing ``Z`` needs a swap. We
    always normalize to UTC-aware so comparisons against ``_utcnow``
    are well-defined.
    """
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)] + "…"


# --------------------------------------------------------------------- service

class DailyReviewService:
    """Runs the end-of-day review fork on a slow cadence."""

    def __init__(
        self,
        *,
        memory_store: MemoryStore,
        skill_history_store: "SkillHistoryStore",
        usage_store: "SkillUsageStore",
        agent: "AgentLoop",
        warmup_seconds: int = 3600,
        interval_seconds: int = 24 * 3600,
        lookback_seconds: int = 24 * 3600,
        max_memory_rows: int = 60,
        max_skill_history_rows: int = 50,
        max_skills_in_usage_table: int = 20,
        max_iterations: int = 8,
        enabled: bool = True,
        gateway_manager: "Optional[GatewayManager]" = None,
        push_target_id: Optional[int] = None,
        action_log: Optional[ReviewActionLog] = None,
    ) -> None:
        if warmup_seconds < 0:
            raise ValueError("warmup_seconds must be >= 0")
        if interval_seconds < 60:
            raise ValueError("interval_seconds must be >= 60")
        if lookback_seconds < 60:
            raise ValueError("lookback_seconds must be >= 60")
        self._memory_store = memory_store
        self._skill_history = skill_history_store
        self._usage_store = usage_store
        self._agent = agent
        self._warmup = warmup_seconds
        self._interval = interval_seconds
        self._lookback = lookback_seconds
        self._max_memory_rows = max_memory_rows
        self._max_skill_history_rows = max_skill_history_rows
        self._max_skills_in_usage_table = max_skills_in_usage_table
        self._max_iterations = max_iterations
        self._enabled = enabled
        # v0.43 Phase B+ — optional push of the daily report to an IM
        # gateway. Both inputs must be set to enable; otherwise the
        # service runs silently and the operator polls /api/review.
        self._gateway_manager = gateway_manager
        self._push_target_id = push_target_id
        self._action_log = action_log
        self._task: Optional[asyncio.Task[None]] = None
        self._stop = asyncio.Event()
        self._last_run_at: Optional[str] = None
        self._last_summary: Optional[dict] = None
        self._last_push_status: Optional[dict] = None
        self._last_graph_review: Optional[dict[str, Any]] = None

    # ------------------------------------------------------------- properties

    @property
    def last_run_at(self) -> Optional[str]:
        return self._last_run_at

    @property
    def last_summary(self) -> Optional[dict]:
        return self._last_summary

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def push_target_id(self) -> Optional[int]:
        return self._push_target_id

    @property
    def last_push_status(self) -> Optional[dict]:
        """``{ok, target, error}`` from the last attempted push, or None."""
        return self._last_push_status

    @property
    def last_graph_review(self) -> Optional[dict[str, Any]]:
        return self._last_graph_review

    @property
    def action_log(self) -> Optional[ReviewActionLog]:
        return self._action_log

    # ---------------------------------------------------------- lifecycle

    async def start(self) -> None:
        if not self._enabled:
            logger.info("[daily_review] service disabled; not starting")
            return
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="zlagent-daily-review")
        logger.info(
            "[daily_review] service started (warmup={}s, interval={}s,"
            " lookback={}s)",
            self._warmup, self._interval, self._lookback,
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        self._task = None
        logger.info("[daily_review] service stopped")

    # ------------------------------------------------------------ run_once

    async def run_once(self) -> Optional[dict]:
        """Collect inputs, drive the review fork, store the summary.

        Returns the same dict shape stored under ``last_summary`` (so
        the REST surface can hand it back without an extra read), or
        ``None`` if the run failed before producing any output. All
        exceptions are swallowed and logged — the service is best-effort.
        """
        try:
            inputs = self._collect_inputs(now=_utcnow())
            graph_review = self._build_graph_review(inputs)
            self._last_graph_review = graph_review
            user_block = self._build_user_block(inputs, graph_review=graph_review)
            # v0.43 Phase B+ — snapshot memory + skill_history BEFORE
            # the review so we can diff the deltas afterwards and
            # write them to the audit log. Cheap (one .list() call +
            # one wc -l on the JSONL).
            mem_before = self._snapshot_memory()
            history_count_before = self._snapshot_history_count()
            review_result = await self._agent.run_end_of_day_review(
                user_block, max_iterations=self._max_iterations,
            )
            mem_after = self._snapshot_memory()
            history_count_after = self._snapshot_history_count()
            new_memory_ids, archived_memory_ids = _diff_memory_snapshot(
                mem_before, mem_after,
            )
            new_skill_history = self._read_new_history_entries(
                history_count_before, history_count_after,
            )
            now = _utcnow()
            self._last_run_at = now.isoformat(timespec="seconds")
            run_id: Optional[int] = None
            if self._action_log is not None and bool(review_result.get("ok")):
                try:
                    run_id = self._action_log.append_run(
                        started_at=inputs["window_start"],
                        finished_at=now.isoformat(timespec="seconds"),
                        review_ok=True,
                        skill_calls=int(review_result.get("skill_calls") or 0),
                        memory_calls=int(review_result.get("memory_calls") or 0),
                        final_text=str(review_result.get("final_text") or ""),
                        new_memory_ids=new_memory_ids,
                        archived_memory_ids=archived_memory_ids,
                        skill_history_added=new_skill_history,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "[daily_review] action_log.append_run failed: {}", exc,
                    )
            summary = {
                "started_at": inputs["window_start"],
                "finished_at": now.isoformat(timespec="seconds"),
                "lookback_seconds": self._lookback,
                "inputs": {
                    "agent_note_count": len(inputs["agent_notes"]),
                    "user_fact_count": len(inputs["user_facts"]),
                    "skill_history_count": len(inputs["skill_history"]),
                    "graph_candidate_count": len((graph_review or {}).get("candidates") or []),
                    "skills_with_usage": len(
                        (inputs["skill_usage"] or {}).get("top_rows") or []
                    ),
                    "skill_usage_source": (
                        inputs["skill_usage"] or {}
                    ).get("source", "snapshot"),
                },
                "graph_review": graph_review,
                "review": review_result,
                "actions": {
                    "run_id": run_id,
                    "new_memory_ids": new_memory_ids,
                    "archived_memory_ids": archived_memory_ids,
                    "skill_history_added_count": len(new_skill_history),
                },
            }
            self._last_summary = summary
            logger.info(
                "[daily_review] notes={} facts={} skill_evts={} →"
                " skill_calls={} memory_calls={} new_mem={} arch_mem={}"
                " new_skill_evts={} run_id={}",
                len(inputs["agent_notes"]),
                len(inputs["user_facts"]),
                len(inputs["skill_history"]),
                review_result.get("skill_calls", 0),
                review_result.get("memory_calls", 0),
                len(new_memory_ids), len(archived_memory_ids),
                len(new_skill_history), run_id,
            )
            # v0.43 Phase B+ — fire-and-forget IM push. Failures are
            # captured in last_push_status and logged but never
            # propagate; the summary is still returned to the caller.
            push_status = await self._maybe_push(summary)
            if push_status is not None:
                self._last_push_status = push_status
                summary["push"] = push_status
            return summary
        except Exception as exc:  # noqa: BLE001
            logger.exception("[daily_review] run_once failed: {}", exc)
            return None

    # ====================================================================
    # rollback
    # ====================================================================

    def rollback_run(
        self, run_id: int, *, note: Optional[str] = None,
    ) -> dict[str, Any]:
        """Reverse memory effects of run ``run_id``. Idempotent.

        Memory rollback is automatic (archive newly-created rows,
        unarchive newly-archived rows). Skill files are NOT auto-
        reverted because reversing arbitrary edits needs holding the
        pre-edit content; the returned ``skill_history_added`` list
        is informational so the operator can ``git revert`` or use
        the existing skill REST.
        """
        if self._action_log is None:
            return {"ok": False, "reason": "action_log not wired"}
        row = self._action_log.get_run(int(run_id))
        if row is None:
            return {"ok": False, "reason": f"run #{run_id} not found"}
        if row.get("rolled_back_at"):
            return {
                "ok": False, "reason": "already rolled back",
                "rolled_back_at": row["rolled_back_at"],
            }
        new_ids = [int(i) for i in row.get("new_memory_ids") or []]
        arch_ids = [int(i) for i in row.get("archived_memory_ids") or []]
        archived_now: list[int] = []
        unarchived_now: list[int] = []
        errors: list[str] = []
        # Re-archive the rows the review created.
        for mid in new_ids:
            try:
                if self._memory_store.archive(mid):
                    archived_now.append(mid)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"archive#{mid}: {exc}")
        # Un-archive the rows the review archived (typically via
        # consolidate-merge).
        for mid in arch_ids:
            try:
                if self._memory_store.unarchive(mid):
                    unarchived_now.append(mid)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"unarchive#{mid}: {exc}")
        # Tombstone the log entry so future calls see "already rolled back".
        try:
            self._action_log.append_rollback(int(run_id), note=note)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"tombstone: {exc}")
        return {
            "ok": True,
            "run_id": int(run_id),
            "memory_archived": archived_now,
            "memory_unarchived": unarchived_now,
            "skill_history_added": row.get("skill_history_added") or [],
            "errors": errors,
        }

    def _snapshot_memory(self) -> dict[int, bool]:
        """Return ``{id: archived_flag}`` for every memory row, archived or not."""
        try:
            rows = self._memory_store.list(include_archived=True, limit=None)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[daily_review] memory snapshot failed: {}", exc)
            return {}
        snap: dict[int, bool] = {}
        for r in rows:
            try:
                rid = int(r.get("id"))
            except (TypeError, ValueError):
                continue
            snap[rid] = bool(r.get("archived"))
        return snap

    def _snapshot_history_count(self) -> int:
        """Cheap line count of skill_history.jsonl — used for diff start index."""
        path = getattr(self._skill_history, "path", None)
        if path is None:
            return 0
        try:
            p = path  # ``Path`` from SkillHistoryStore.path
            if not p.is_file():
                return 0
            with open(p, "rb") as f:
                # We need the line count, not file size, because a
                # partial trailing line shouldn't be over-counted.
                count = 0
                for _ in f:
                    count += 1
            return count
        except Exception as exc:  # noqa: BLE001
            logger.warning("[daily_review] history count failed: {}", exc)
            return 0

    def _read_new_history_entries(
        self, before_count: int, after_count: int,
    ) -> list[dict[str, Any]]:
        """Return the slice of skill_history.jsonl added by this run."""
        if after_count <= before_count:
            return []
        path = getattr(self._skill_history, "path", None)
        if path is None or not path.is_file():
            return []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            logger.warning("[daily_review] history slice read failed: {}", exc)
            return []
        new_slice = lines[before_count:after_count]
        out: list[dict[str, Any]] = []
        for line in new_slice:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            # Keep only the small, identifying fields — full diffs stay
            # in skill_history.jsonl for operator forensics.
            out.append({
                "timestamp": obj.get("timestamp"),
                "skill_name": obj.get("skill_name"),
                "action": obj.get("action"),
                "actor": obj.get("actor"),
            })
        return out

    # ====================================================================
    # input collection
    # ====================================================================

    def _collect_inputs(self, *, now: datetime) -> dict[str, Any]:
        """Gather the past-N-h memory / skill data the LLM will read.

        Pure read; returns a structured dict. Collection is best-effort
        per source — if the skill history file is missing or malformed
        we just return an empty list for that source.
        """
        window_start = now - timedelta(seconds=self._lookback)
        agent_notes = self._collect_memory_window(
            kind=KIND_AGENT_NOTE, window_start=window_start,
        )
        user_facts = self._collect_memory_window(
            kind=KIND_USER_FACT, window_start=window_start,
        )
        skill_history = self._collect_skill_history_window(
            window_start=window_start,
        )
        skill_usage = self._collect_skill_usage(window_start=window_start)
        return {
            "window_start": window_start.isoformat(timespec="seconds"),
            "window_end": now.isoformat(timespec="seconds"),
            "agent_notes": agent_notes,
            "user_facts": user_facts,
            "skill_history": skill_history,
            "skill_usage": skill_usage,
        }

    def _collect_memory_window(
        self, *, kind: str, window_start: datetime,
    ) -> list[dict[str, Any]]:
        """Return memory rows of ``kind`` whose ``created_at`` >= window_start.

        ``MemoryStore.list`` doesn't accept a ``since=`` filter, so we
        pull the most-recent ``max_memory_rows`` and filter in Python.
        For a single-user instance with order-of-100 rows total this is
        cheap and avoids touching the store API.
        """
        try:
            rows = self._memory_store.list(
                kind=kind, limit=self._max_memory_rows,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[daily_review] memory_store.list({}) failed: {}", kind, exc,
            )
            return []
        out: list[dict[str, Any]] = []
        for row in rows:
            created = _parse_iso(row.get("created_at"))
            if created is None or created < window_start:
                continue
            out.append({
                "id": row.get("id"),
                "content": str(row.get("content") or ""),
                "source": row.get("source"),
                "pinned": bool(row.get("pinned")),
                "created_at": row.get("created_at"),
                "recall_count": int(row.get("recall_count") or 0),
            })
        return out

    def _collect_skill_history_window(
        self, *, window_start: datetime,
    ) -> list[dict[str, Any]]:
        try:
            entries = self._skill_history.list_entries(
                limit=self._max_skill_history_rows,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[daily_review] skill_history.list_entries failed: {}", exc,
            )
            return []
        out: list[dict[str, Any]] = []
        for entry in entries:
            ts = _parse_iso(entry.get("timestamp"))
            if ts is None or ts < window_start:
                continue
            out.append({
                "timestamp": entry.get("timestamp"),
                "skill_name": entry.get("skill_name"),
                "action": entry.get("action"),
                "actor": entry.get("actor"),
            })
        return out

    def _collect_skill_usage(
        self, *, window_start: datetime,
    ) -> dict[str, Any]:
        """Per-skill activity stats over the past lookback window.

        Prefers the v0.43-Phase-B+ ``usage_events.jsonl`` (real
        time-windowed counts via :meth:`SkillUsageStore.events_since`).
        Falls back to the aggregate snapshot when the events file is
        empty / missing — keeps Phase B working in environments where
        the events log hasn't accumulated yet.

        Returns a dict with ``source`` ("events" or "snapshot") so
        :meth:`_build_user_block` can label the section accurately,
        plus ``top_rows`` (the top-N entries to render).
        """
        # 1) try event log first — gives real "past 24 h" counts
        try:
            events = self._usage_store.events_since(window_start)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[daily_review] events_since failed: {}", exc)
            events = []
        if events:
            agg: dict[str, dict[str, int]] = {}
            for ev in events:
                name = ev.get("skill_name") or ""
                if not name:
                    continue
                slot = agg.setdefault(
                    name,
                    {"use_count": 0, "view_count": 0, "patch_count": 0},
                )
                kind = ev.get("kind")
                if kind == "use":
                    slot["use_count"] += 1
                elif kind == "view":
                    slot["view_count"] += 1
                elif kind == "patch":
                    slot["patch_count"] += 1
            rows: list[dict[str, Any]] = []
            for name, counts in agg.items():
                rows.append({
                    "skill_name": name,
                    "use_count": counts["use_count"],
                    "view_count": counts["view_count"],
                    "patch_count": counts["patch_count"],
                })
            rows.sort(
                key=lambda r: (
                    r["use_count"] + r["view_count"], r["patch_count"],
                ),
                reverse=True,
            )
            return {
                "source": "events",
                "top_rows": rows[: self._max_skills_in_usage_table],
                "total_events": len(events),
            }

        # 2) fallback to aggregate snapshot (legacy behaviour)
        try:
            all_records = self._usage_store.all()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[daily_review] usage_store.all failed: {}", exc)
            return {"source": "snapshot", "top_rows": [], "total_events": 0}
        rows = []
        for name, rec in all_records.items():
            use_count = int(rec.get("use_count") or 0)
            view_count = int(rec.get("view_count") or 0)
            patch_count = int(rec.get("patch_count") or 0)
            if use_count == 0 and view_count == 0 and patch_count == 0:
                continue
            rows.append({
                "skill_name": name,
                "use_count": use_count,
                "view_count": view_count,
                "patch_count": patch_count,
                "last_used_at": rec.get("last_used_at"),
                "last_viewed_at": rec.get("last_viewed_at"),
                "last_patched_at": rec.get("last_patched_at"),
            })
        rows.sort(
            key=lambda r: (
                r["use_count"] + r["view_count"], r["patch_count"],
            ),
            reverse=True,
        )
        return {
            "source": "snapshot",
            "top_rows": rows[: self._max_skills_in_usage_table],
            "total_events": 0,
        }

    # ====================================================================
    # prompt construction
    # ====================================================================

    def _build_user_block(self, inputs: dict[str, Any]) -> str:
        """Render the structured user message the review LLM reads.

        Sections mirror the END_OF_DAY_REVIEW_PROMPT's "工作输入" list
        so the model sees data in the order it expects:

          1. agent_notes  (L2 — abstracted underlying logic)
          2. user_facts   (L3 — operator-grounded facts)
          3. skill_history (mutations in the window)
          4. skill_usage   (top-N hot skills snapshot)
        """
        parts: list[str] = []
        win_start = inputs.get("window_start", "?")
        win_end = inputs.get("window_end", "?")
        parts.append(
            f"## 复盘窗口\n"
            f"从 {win_start} 到 {win_end} (lookback="
            f"{self._lookback}s)"
        )

        agent_notes = inputs.get("agent_notes", [])
        if agent_notes:
            lines = [f"## 过去 24h L2 agent_note 写入 ({len(agent_notes)} 条)"]
            for row in agent_notes:
                lines.append(
                    f"- [{row['id']}] (src={row.get('source')},"
                    f" recalled×{row.get('recall_count', 0)})"
                    f" {_truncate(row['content'], 240)}"
                )
            parts.append("\n".join(lines))
        else:
            parts.append("## 过去 24h L2 agent_note 写入\n(空 — 这一天没有新增底层逻辑)")

        user_facts = inputs.get("user_facts", [])
        if user_facts:
            lines = [f"## 过去 24h L3 user_fact 写入 ({len(user_facts)} 条)"]
            for row in user_facts:
                lines.append(
                    f"- [{row['id']}] (src={row.get('source')})"
                    f" {_truncate(row['content'], 240)}"
                )
            parts.append("\n".join(lines))
        else:
            parts.append("## 过去 24h L3 user_fact 写入\n(空)")

        skill_history = inputs.get("skill_history", [])
        if skill_history:
            lines = [
                f"## 过去 24h skill 文件变更 ({len(skill_history)} 条)"
            ]
            for entry in skill_history:
                lines.append(
                    f"- {entry.get('timestamp')} "
                    f"{entry.get('skill_name')}"
                    f" · {entry.get('action')}"
                    f" (actor={entry.get('actor')})"
                )
            parts.append("\n".join(lines))
        else:
            parts.append("## 过去 24h skill 文件变更\n(空)")

        skill_usage = inputs.get("skill_usage") or {}
        top_rows = skill_usage.get("top_rows") or []
        source = skill_usage.get("source", "snapshot")
        if top_rows:
            if source == "events":
                # v0.43 Phase B+ — actual past-window counts.
                heading = (
                    f"## skill 调用热度 (窗口内, top {len(top_rows)}, 共"
                    f" {skill_usage.get('total_events', 0)} 事件)"
                )
                line_fmt = (
                    "- {name}: use={use} view={view} patch={patch}"
                )
            else:
                # Snapshot fallback — labels honestly so the LLM knows
                # this isn't a clean "today" slice.
                heading = (
                    f"## skill 调用热度快照 (top {len(top_rows)}, 全量"
                    " 累计, 不只是窗口)"
                )
                line_fmt = (
                    "- {name}: use={use} view={view} patch={patch}"
                    " last_used={last}"
                )
            lines = [heading]
            for row in top_rows:
                lines.append(line_fmt.format(
                    name=row["skill_name"],
                    use=row["use_count"],
                    view=row["view_count"],
                    patch=row["patch_count"],
                    last=row.get("last_used_at"),
                ))
            parts.append("\n".join(lines))
        else:
            parts.append(
                "## skill 调用热度\n(空 — 整库目前 0 命中,"
                " 可能还没接入实际流量)"
            )

        if graph_review:
            parts.append(self._format_graph_review_block(graph_review))
        parts.append(
            "## 任务\n"
            "请按 system prompt 的「日终 / 阶段完成复盘」流程评估上面"
            " 4 段输入：\n"
            "  - 哪些 L2 trigger/criterion 已经稳定到值得 skill_manage"
            "(action='create')？\n"
            "  - 哪些已有 skill 在 history 里反复被 patch 暴露的 pit"
            " 应该 skill_manage(action='patch') 固化？\n"
            "  - 是否有 memory 条目语义重叠值得 memory_manage"
            "(action='consolidate')？\n"
            "  - GraphRAG 候选里哪些要入图，哪些要等二次复盘？\n"
            "如果都没有，回复一句话「无需更新」就行；不要硬造动作。"
        )
        return "\n\n".join(parts)

    # ====================================================================
    # GraphRAG review
    # ====================================================================

    def _build_graph_review(self, inputs: dict[str, Any]) -> dict[str, Any]:
        graph_items: list[dict[str, Any]] = []
        for kind in ("default", "memory", "paper"):
            try:
                bundle = collect_graph_records(knowledge_base_id=kind)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[daily_review] collect_graph_records({}) failed: {}", kind, exc)
                continue
            for record in bundle.records[:5]:
                graph_items.append(
                    {
                        "id": record.id,
                        "source_type": record.source_type,
                        "knowledge_base_id": record.knowledge_base_id,
                        "title": record.title,
                        "summary": record.summary,
                        "tags": record.tags,
                        "pending_review": kind != "default",
                    }
                )
        candidates = []
        for row in inputs.get("agent_notes", [])[:6]:
            candidates.append({
                "kind": "agent_note",
                "id": row.get("id"),
                "content": _truncate(str(row.get("content") or ""), 160),
            })
        for row in inputs.get("user_facts", [])[:6]:
            candidates.append({
                "kind": "user_fact",
                "id": row.get("id"),
                "content": _truncate(str(row.get("content") or ""), 160),
            })
        candidates.extend(graph_items)
        review_summary = {
            "status": "queued",
            "reason": "awaiting LLM review in nightly cycle",
            "candidates": candidates,
            "graph_items": graph_items,
            "action": "review_then_insert",
        }
        return review_summary

    def _format_graph_review_block(self, graph_review: dict[str, Any]) -> str:
        candidates = graph_review.get("candidates") or []
        graph_items = graph_review.get("graph_items") or []
        lines = [
            "## GraphRAG 夜间候选",
            f"- 状态: {graph_review.get('status', 'queued')}",
            f"- 说明: {graph_review.get('reason', '')}",
            f"- 候选总数: {len(candidates)}",
            f"- 图谱条目: {len(graph_items)}",
            "- 处理策略: 先给 LLM 审核，再决定是否入图 / 合并 / 延后复盘",
        ]
        if graph_items:
            lines.append("### 候选图谱条目")
            for item in graph_items[:10]:
                lines.append(
                    f"- [{item.get('source_type')}] {item.get('title')} :: {item.get('summary')}"
                )
        return "\n".join(lines)

    # ====================================================================
    # IM push
    # ====================================================================

    async def _maybe_push(
        self, summary: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        """If a push target is configured, dispatch the daily report.

        Returns a status dict (``{ok, target, error}``) on attempt, or
        ``None`` if push is unconfigured (no gateway / no target id).
        Never raises — failures are captured in the returned status.
        """
        if self._gateway_manager is None or self._push_target_id is None:
            return None
        try:
            row_id = int(self._push_target_id)
        except (TypeError, ValueError):
            return {
                "ok": False, "target": None,
                "error": f"invalid push_target_id={self._push_target_id!r}",
            }
        # Resolve the delivery target row to a live DeliveryTarget. We
        # take a fresh session — the service runs in a long-lived task
        # so each push must own its own DB scope.
        try:
            with session_scope() as session:
                row = session.get(DeliveryTargetRow, row_id)
                if row is None:
                    return {
                        "ok": False, "target": None,
                        "error": f"delivery_target #{row_id} not found",
                    }
                if not row.enabled:
                    return {
                        "ok": False,
                        "target": f"#{row_id}",
                        "error": f"delivery_target #{row_id} is disabled",
                    }
                target = DeliveryTarget(
                    platform=row.platform,
                    target_type=row.target_type,
                    target_id=row.target_id,
                    display_name=row.display_name or row.target_id,
                )
                describe = target.describe()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[daily_review] push target resolve failed: {}", exc,
            )
            return {
                "ok": False, "target": None,
                "error": f"resolve failed: {exc}",
            }
        text = self._format_push_text(summary)
        try:
            await self._gateway_manager.dispatch(
                OutgoingMessage(target=target, text=text),
            )
            logger.info("[daily_review] pushed report to {}", describe)
            return {"ok": True, "target": describe, "error": None}
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[daily_review] push to {} failed: {}", describe, exc,
            )
            return {"ok": False, "target": describe, "error": str(exc)}

    def _format_push_text(self, summary: dict[str, Any]) -> str:
        """Render the daily-report message for IM delivery.

        Keep it short — typical IM clients clip long messages and the
        operator only wants to know "did the review do anything weird
        today?". The full payload is always accessible via
        ``GET /api/review/state`` for deep dives.
        """
        inputs = summary.get("inputs", {})
        review = summary.get("review", {})
        ok = bool(review.get("ok"))
        skill_calls = int(review.get("skill_calls") or 0)
        memory_calls = int(review.get("memory_calls") or 0)
        head = (
            f"[日终复盘] {summary.get('finished_at', '?')}\n"
            f"  输入: notes={inputs.get('agent_note_count', 0)}"
            f" facts={inputs.get('user_fact_count', 0)}"
            f" skill_evts={inputs.get('skill_history_count', 0)}"
            f" 热门 skill={inputs.get('skills_with_usage', 0)}"
        )
        if not ok:
            reason = review.get("reason") or "unknown error"
            return f"{head}\n  ⚠ 复盘未执行: {reason}"
        if skill_calls == 0 and memory_calls == 0:
            return f"{head}\n  ✔ 无需更新 (今日无 skill / memory 改动)"
        body_lines = [head, f"  ✔ skill_calls={skill_calls} memory_calls={memory_calls}"]
        final_text = (review.get("final_text") or "").strip()
        if final_text:
            # IM gateways clip long messages; cap at ~280 chars to
            # keep the report scannable.
            if len(final_text) > 280:
                final_text = final_text[:279] + "…"
            body_lines.append(f"  → {final_text}")
        return "\n".join(body_lines)

    # ====================================================================
    # internals
    # ====================================================================

    async def _run(self) -> None:
        # Warmup — gives the rest of FastAPI lifespan a chance to settle
        # before we start poking the LLM. Cancellable via stop().
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=self._warmup)
            return
        except asyncio.TimeoutError:
            pass

        while not self._stop.is_set():
            try:
                await self.run_once()
            except Exception as exc:  # noqa: BLE001 — never let aux task die
                logger.exception("[daily_review] tick failed: {}", exc)
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._interval,
                )
                return
            except asyncio.TimeoutError:
                continue
