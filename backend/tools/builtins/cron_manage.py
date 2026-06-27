"""cron_manage: agent-managed scheduled tasks (confirm tier).

This is the v0.8 surface that lets the LLM stand up new cron jobs from a
natural-language user request (e.g. "每天 8 点给我推送 cs.AI 论文"). It
mirrors the workflow Hermes Agent ships in its own ``cronjob`` tool:

  1. agent receives a recurring-task request,
  2. agent calls ``skill_manage(action='create')`` to author the procedure,
  3. agent calls ``cron_manage(action='create')`` to wire the schedule.

Critical design choices that match Hermes:

* The ``deliver_to_current_chat`` flag (default ``True``) makes the cron
  push back to **the chat the user is currently messaging from**, removing
  the friction of asking the user for a delivery target every time. The
  current chat is read from
  :func:`backend.agent.context.current_turn_context`; if no turn
  context is set (e.g. a smoke test driving the tool directly) the agent
  must supply ``delivery_target_id`` explicitly.

* When the resolved current-chat target has no row in
  ``delivery_targets``, we auto-create one (matching how a human operator
  would have called ``POST /api/delivery-targets`` first).

* All actions are gated by the v0.6 IM confirmation flow because cron
  jobs *will* run autonomously and consume credits — every create / edit
  / delete must be approved by the user via yes/no.

Actions: ``create / list / update / pause / resume / remove / run``.
The schema is intentionally compressed onto a single tool to keep the
LLM's tool list short (Hermes does the same).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from loguru import logger

from ...agent.context import current_turn_context
from ...db.models import CronJob, DeliveryTarget as DeliveryTargetRow
from ...db.session import session_scope
from ..base import Tool, ToolPermission, ToolResult

# v0.45 — actions that don't mutate state and shouldn't trigger the
# confirm yes/no prompt. The runner consults this via
# :meth:`Tool.is_action_read_only`.
_CRON_READ_ONLY_ACTIONS = frozenset({"list"})

# Lowercase slugs only, matching skill_manage's convention so cron job
# names round-trip cleanly into log lines and URLs.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
SHANGHAI_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")

# Human shorthand → cron expression. Hermes accepts "30m" / "every 2h" /
# raw cron; we mirror a small subset since the LLM is fully capable of
# producing 5-field expressions itself, but we do want a pleasant fallback.
_SHORTHAND = {
    "every minute": "*/1 * * * *",
    "hourly": "0 * * * *",
    "every hour": "0 * * * *",
    "daily": "0 8 * * *",
    "every day": "0 8 * * *",
    "weekly": "0 8 * * 1",
    "every week": "0 8 * * 1",
}


def _normalise_cron_expr(expr: str) -> Optional[str]:
    raw = (expr or "").strip()
    if not raw:
        return None
    canonical = raw.lower()
    if canonical in _SHORTHAND:
        return _SHORTHAND[canonical]
    # "every Nh" / "every Nm"
    m = re.match(r"^every\s+(\d+)\s*([smhd])$", canonical)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        if unit == "m" and 1 <= n <= 59:
            return f"*/{n} * * * *"
        if unit == "h" and 1 <= n <= 23:
            return f"0 */{n} * * *"
        if unit == "d" and n == 1:
            return "0 8 * * *"
    return raw  # assume already a 5-field cron expression


def _validate_cron_expr(expr: str) -> Optional[str]:
    try:
        croniter(expr)
        return None
    except (ValueError, KeyError) as exc:
        return str(exc)


class CronManageTool(Tool):
    name = "cron_manage"
    description = (
        "Manage cron jobs for reminders and recurring tasks. Use create for new schedules, list for inspection, and update/pause/resume/remove for existing jobs. "
        "Simple reminders can run once; recurring tasks may reference an optional skill."
    )
    permission = ToolPermission.CONFIRM
    is_read_only = False
    is_concurrency_safe = False
    is_destructive = True
    max_result_chars = 8_000
    search_hint = "cron schedule recurring task reminder automation pause resume"

    def __init__(self, *, skill_loader: Optional[Any] = None) -> None:
        # v0.45 — optional ``skill_loader`` lets ``_create`` reject a
        # ``skill_hint`` that doesn't actually point at a known skill.
        # Without it the tool still works (validation degrades to a
        # no-op) so smoke tests that construct ``CronManageTool()``
        # bare keep passing.
        self._skill_loader = skill_loader

    def is_action_read_only(self, arguments: dict[str, Any] | None) -> bool:
        """v0.45 — ``list`` is the only read-only action; everything
        else (``create`` / ``update`` / ``pause`` / ``resume`` /
        ``remove`` / ``run``) mutates the cron table and stays gated
        by the confirm tier.
        """
        if not isinstance(arguments, dict):
            return False
        action = str(arguments.get("action") or "").strip().lower()
        return action in _CRON_READ_ONLY_ACTIONS

    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "list", "update", "pause", "resume", "remove", "run"],
                "description": "Which mutation / inspection to perform.",
            },
            "job_id": {
                "type": "integer",
                "description": "Existing cron job id (required for update/pause/resume/remove/run).",
            },
            "name": {
                "type": "string",
                "description": (
                    "Lowercase-slug job name, e.g. 'arxiv-daily'."
                    " Required for create. Same constraints as skill_name."
                ),
            },
            "cron_expr": {
                "type": "string",
                "description": (
                    "Standard 5-field cron expression, e.g. '0 8 * * *'."
                    " Shorthand 'daily' / 'hourly' / 'every 30m' /"
                    " 'every 2h' also accepted. Beijing time."
                ),
            },
            "instruction": {
                "type": "string",
                "description": "Instruction to run when the cron fires.",
            },
            "skill_hint": {
                "type": "string",
                "description": "Optional skill name to load before the job runs.",
            },
            "deliver_to_current_chat": {
                "type": "boolean",
                "description": (
                    "Default true. When true, the job's pushes land in the"
                    " same chat the user is messaging from right now. Set"
                    " to false ONLY if the user explicitly named a"
                    " different delivery target."
                ),
                "default": True,
            },
            "delivery_target_id": {
                "type": "integer",
                "description": (
                    "Existing delivery_target row id. Use only when"
                    " `deliver_to_current_chat=false` and the user asked"
                    " to push somewhere other than the current chat."
                ),
            },
            "enabled": {
                "type": "boolean",
                "description": "Default true. Set false to create a paused job.",
                "default": True,
            },
            "run_once": {
                "type": "boolean",
                "description": (
                    "Default false. Set true for one-shot reminders/alarms"
                    " such as '3点提醒我洗澡'. After a successful run the job"
                    " disables itself automatically."
                ),
                "default": False,
            },
            "pre_script_path": {
                "type": "string",
                "description": (
                    "Optional workspace-relative path to a .py / .sh script"
                    " that will be executed before the LLM is called every"
                    " tick; the script's stdout is spliced into the user"
                    " message as ground-truth data. Use this when the cron"
                    " job depends on **fresh real data** the LLM could not"
                    " possibly know (live feeds, system metrics, repo diffs)."
                    " Path traversal is rejected — the path must resolve"
                    " under the workspace root and end in `.py` or `.sh`."
                ),
            },
            "pre_script_timeout_seconds": {
                "type": "integer",
                "minimum": 1, "maximum": 300,
                "description": (
                    "Hard timeout for pre_script execution. Default 30s,"
                    " maximum 300s. Choose conservatively — a hung script"
                    " delays its tick by exactly this many seconds."
                ),
                "default": 30,
            },
        },
        "required": ["action"],
    }

    # -- entry point ---------------------------------------------------

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        action = str(arguments.get("action") or "").strip().lower()
        if not action:
            return ToolResult(ok=False, content="", error="action is required")
        try:
            if action == "create":
                return self._create(arguments)
            if action == "list":
                return self._list()
            if action == "update":
                return self._update(arguments)
            if action == "pause":
                return self._toggle(arguments, enabled=False)
            if action == "resume":
                return self._toggle(arguments, enabled=True)
            if action == "remove":
                return self._remove(arguments)
            if action == "run":
                return ToolResult(
                    ok=False, content="",
                    error=(
                        "action='run' is not supported through cron_manage in"
                        " call POST /api/cron/{id}/run from the ops"
                        " panel or wait for the next scheduled tick."
                    ),
                )
        except Exception as exc:  # noqa: BLE001 - surface nicely
            logger.exception("cron_manage action={} crashed", action)
            return ToolResult(ok=False, content="", error=f"{type(exc).__name__}: {exc}")
        return ToolResult(ok=False, content="", error=f"unknown action: {action}")

    # -- create --------------------------------------------------------

    def _create(self, arguments: dict[str, Any]) -> ToolResult:
        name = str(arguments.get("name") or "").strip()
        cron_expr_raw = str(arguments.get("cron_expr") or "").strip()
        instruction = str(arguments.get("instruction") or "").strip()
        skill_hint = (arguments.get("skill_hint") or None) or None
        if isinstance(skill_hint, str):
            skill_hint = skill_hint.strip() or None
        deliver_to_current = bool(arguments.get("deliver_to_current_chat", True))
        explicit_target_id = arguments.get("delivery_target_id")
        enabled = bool(arguments.get("enabled", True))
        run_once = bool(arguments.get("run_once", False))
        pre_script_path = arguments.get("pre_script_path")
        if isinstance(pre_script_path, str):
            pre_script_path = pre_script_path.strip() or None
        else:
            pre_script_path = None
        try:
            pre_script_timeout = int(arguments.get("pre_script_timeout_seconds") or 30)
        except (TypeError, ValueError):
            return ToolResult(
                ok=False, content="",
                error="pre_script_timeout_seconds must be an integer",
            )
        if not (1 <= pre_script_timeout <= 300):
            return ToolResult(
                ok=False, content="",
                error="pre_script_timeout_seconds must be between 1 and 300",
            )

        if not name:
            return ToolResult(ok=False, content="", error="name is required")
        if not _NAME_RE.match(name):
            return ToolResult(
                ok=False, content="",
                error=f"name {name!r} must match [a-z0-9][a-z0-9._-]{{0,127}}",
            )
        if not cron_expr_raw:
            return ToolResult(ok=False, content="", error="cron_expr is required")
        if not instruction:
            return ToolResult(ok=False, content="", error="instruction is required")

        # v0.45 — validate skill_hint against the live SkillLoader so
        # the LLM can't silently file a cron that fires next morning
        # against a skill name that doesn't exist (which then dies on
        # the next tick with "skill not found"). The check is lenient:
        # if no skill_loader was injected, we skip — preserves test-
        # harness ergonomics for callers that build the tool bare.
        if skill_hint and self._skill_loader is not None:
            try:
                manifest = self._skill_loader.get(skill_hint)
            except Exception:  # noqa: BLE001
                manifest = None
            if manifest is None:
                try:
                    known = sorted(
                        m.id for m in self._skill_loader.list()
                    )[:10]
                except Exception:  # noqa: BLE001
                    known = []
                hint = (
                    f"; known skills: {', '.join(known)}" if known else ""
                )
                return ToolResult(
                    ok=False, content="",
                    error=(
                        f"skill_hint {skill_hint!r} does not match any"
                        f" registered skill{hint}. Run skill_manage"
                        " action='list' to see available names, or"
                        " omit skill_hint for a plain instruction-only"
                        " cron."
                    ),
                )

        cron_expr = _normalise_cron_expr(cron_expr_raw)
        if cron_expr is None:
            return ToolResult(ok=False, content="", error="cron_expr is empty after normalisation")
        cron_err = _validate_cron_expr(cron_expr)
        if cron_err:
            return ToolResult(
                ok=False, content="",
                error=f"invalid cron expression {cron_expr!r}: {cron_err}",
            )

        target_id, target_summary = self._resolve_target(
            deliver_to_current=deliver_to_current,
            explicit_id=explicit_target_id,
        )
        if isinstance(target_id, ToolResult):
            return target_id  # error result

        with session_scope() as session:
            existing = session.query(CronJob).filter(CronJob.name == name).first()
            if existing is not None:
                return ToolResult(
                    ok=False, content="",
                    error=f"cron job {name!r} already exists (id={existing.id}); use action=update",
                )
            job = CronJob(
                name=name,
                cron_expr=cron_expr,
                timezone="Asia/Shanghai",
                instruction=instruction,
                skill_hint=skill_hint,
                delivery_target_id=target_id,
                enabled=enabled,
                run_once=run_once,
                pre_script_path=pre_script_path,
                pre_script_timeout_seconds=pre_script_timeout,
            )
            session.add(job)
            session.flush()
            session.refresh(job)
            job_id = job.id
            next_fire = self._next_fire_iso(cron_expr)

        return ToolResult(
            ok=True,
            content=(
                f"Cron job #{job_id} '{name}' created.\n"
                f"  schedule: {cron_expr} (Asia/Shanghai) — next fire: {next_fire}\n"
                f"  delivery: {target_summary}\n"
                f"  skill_hint: {skill_hint or '(none)'}\n"
                f"  run_once: {run_once}\n"
                f"  pre_script: {pre_script_path or '(none)'}"
                + (f" (timeout={pre_script_timeout}s)" if pre_script_path else "")
                + f"\n  enabled: {enabled}"
            ),
        )

    # -- list / update / toggle / remove -------------------------------

    def _list(self) -> ToolResult:
        with session_scope() as session:
            rows = list(session.query(CronJob).order_by(CronJob.id.asc()).all())
            if not rows:
                return ToolResult(ok=True, content="No cron jobs registered.")
            lines = [f"Cron jobs ({len(rows)}):"]
            for job in rows:
                next_fire = self._next_fire_iso(job.cron_expr) if job.enabled else "(disabled)"
                lines.append(
                    f"  #{job.id} {job.name:<28} {job.cron_expr:<14}"
                    f" skill={job.skill_hint or '-':<20}"
                    f" target={job.delivery_target_id or '-':<4}"
                    f" enabled={job.enabled}"
                    f" once={bool(getattr(job, 'run_once', False))}"
                    f" next={next_fire}"
                )
        return ToolResult(ok=True, content="\n".join(lines))

    def _update(self, arguments: dict[str, Any]) -> ToolResult:
        job_id = arguments.get("job_id")
        if not isinstance(job_id, int):
            return ToolResult(ok=False, content="", error="job_id (int) required for update")

        with session_scope() as session:
            job = session.get(CronJob, job_id)
            if job is None:
                return ToolResult(ok=False, content="", error=f"cron job #{job_id} not found")

            if "cron_expr" in arguments and arguments["cron_expr"] is not None:
                normalised = _normalise_cron_expr(str(arguments["cron_expr"]))
                err = _validate_cron_expr(normalised or "")
                if err:
                    return ToolResult(ok=False, content="", error=f"invalid cron_expr: {err}")
                job.cron_expr = normalised  # type: ignore[assignment]
            if "instruction" in arguments and arguments["instruction"] is not None:
                job.instruction = str(arguments["instruction"]).strip()
            if "skill_hint" in arguments:
                hint = arguments["skill_hint"]
                job.skill_hint = (str(hint).strip() or None) if hint else None
            if "enabled" in arguments and arguments["enabled"] is not None:
                job.enabled = bool(arguments["enabled"])
            if "run_once" in arguments and arguments["run_once"] is not None:
                job.run_once = bool(arguments["run_once"])
            if "pre_script_path" in arguments:
                ps = arguments["pre_script_path"]
                # Pass empty string to clear; non-empty becomes the new path.
                job.pre_script_path = (
                    (str(ps).strip() or None) if ps is not None else None
                )
            if "pre_script_timeout_seconds" in arguments and arguments["pre_script_timeout_seconds"] is not None:
                try:
                    timeout = int(arguments["pre_script_timeout_seconds"])
                except (TypeError, ValueError):
                    return ToolResult(
                        ok=False, content="",
                        error="pre_script_timeout_seconds must be an integer",
                    )
                if not (1 <= timeout <= 300):
                    return ToolResult(
                        ok=False, content="",
                        error="pre_script_timeout_seconds must be between 1 and 300",
                    )
                job.pre_script_timeout_seconds = timeout
            session.flush()
        return ToolResult(ok=True, content=f"Cron job #{job_id} updated.")

    def _toggle(self, arguments: dict[str, Any], *, enabled: bool) -> ToolResult:
        job_id = arguments.get("job_id")
        if not isinstance(job_id, int):
            return ToolResult(ok=False, content="", error="job_id (int) required")
        with session_scope() as session:
            job = session.get(CronJob, job_id)
            if job is None:
                return ToolResult(ok=False, content="", error=f"cron job #{job_id} not found")
            job.enabled = enabled
        return ToolResult(
            ok=True,
            content=f"Cron job #{job_id} {'resumed' if enabled else 'paused'}.",
        )

    def _remove(self, arguments: dict[str, Any]) -> ToolResult:
        job_id = arguments.get("job_id")
        if not isinstance(job_id, int):
            return ToolResult(ok=False, content="", error="job_id (int) required for remove")
        with session_scope() as session:
            job = session.get(CronJob, job_id)
            if job is None:
                return ToolResult(ok=False, content="", error=f"cron job #{job_id} not found")
            name = job.name
            session.delete(job)
        return ToolResult(ok=True, content=f"Cron job #{job_id} '{name}' removed.")

    # -- helpers -------------------------------------------------------

    def _resolve_target(
        self,
        *,
        deliver_to_current: bool,
        explicit_id: Optional[Any],
    ) -> tuple[int, str] | tuple[ToolResult, str]:
        """Decide which delivery_target_id to use, auto-creating if needed."""
        if explicit_id is not None:
            try:
                target_id = int(explicit_id)
            except (TypeError, ValueError):
                return (
                    ToolResult(ok=False, content="", error=f"invalid delivery_target_id: {explicit_id!r}"),
                    "",
                )
            with session_scope() as session:
                row = session.get(DeliveryTargetRow, target_id)
                if row is None:
                    return (
                        ToolResult(
                            ok=False, content="",
                            error=f"delivery_target #{target_id} does not exist",
                        ),
                        "",
                    )
                return target_id, f"#{target_id} {row.platform}:{row.target_id}"

        if not deliver_to_current:
            return (
                ToolResult(
                    ok=False, content="",
                    error=(
                        "deliver_to_current_chat=false but no delivery_target_id"
                        " supplied. Either set deliver_to_current_chat=true or"
                        " pass an existing delivery_target_id."
                    ),
                ),
                "",
            )

        ctx = current_turn_context()
        if ctx is None or ctx.reply_target is None:
            return (
                ToolResult(
                    ok=False, content="",
                    error=(
                        "deliver_to_current_chat=true but no active IM session"
                        " context. Call this tool from inside a real chat turn"
                        " or pass delivery_target_id explicitly."
                    ),
                ),
                "",
            )

        rt = ctx.reply_target
        with session_scope() as session:
            row = (
                session.query(DeliveryTargetRow)
                .filter(
                    DeliveryTargetRow.platform == rt.platform,
                    DeliveryTargetRow.target_type == rt.target_type,
                    DeliveryTargetRow.target_id == rt.target_id,
                )
                .first()
            )
            if row is None:
                row = DeliveryTargetRow(
                    platform=rt.platform,
                    target_type=rt.target_type,
                    target_id=rt.target_id,
                    display_name=rt.display_name or rt.target_id,
                    enabled=True,
                )
                session.add(row)
                session.flush()
                session.refresh(row)
                logger.info(
                    "cron_manage auto-created delivery_target #{} for {}:{}",
                    row.id, rt.platform, rt.target_id,
                )
                summary_prefix = "auto-created"
            else:
                summary_prefix = "existing"
            return row.id, f"{summary_prefix} #{row.id} {rt.platform}:{rt.target_id}"

    @staticmethod
    def _next_fire_iso(cron_expr: str, timezone_name: str = "Asia/Shanghai") -> str:
        try:
            zone = ZoneInfo(timezone_name or "Asia/Shanghai")
        except ZoneInfoNotFoundError:
            zone = SHANGHAI_TZ
        try:
            it = croniter(cron_expr, datetime.now(zone))
            return it.get_next(datetime).isoformat(timespec="minutes")
        except (ValueError, KeyError):
            return "(invalid)"
