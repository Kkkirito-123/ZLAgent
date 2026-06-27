"""send_message: agent proactive outbound message tool (v0.26).

Lets the LLM emit an unsolicited message — "btw I noticed X" — without
waiting for the user to message in or for a cron tick to fire. Closes
the gap between "agent only ever responds" and "true assistant".

Permission tier is **confirm**: every send triggers an IM yes/no
because we are about to push text to a real human channel. The
confirmation prompt also surfaces the operator-facing ``reason`` field
so they can decide quickly without re-reading the full text.

When to use
-----------

* "I noticed your cron 'arxiv-daily' has been failing for 3 days; want
  me to investigate?" (the user did not ask, the agent volunteered).
* "That long-running delegate task just finished — here is the result."
* "Heads up: skill 'meal-checkin' was archived because it had not been
  loaded in 90 days."

When NOT to use
---------------

* Replying to the user's current question — that is the normal
  ``run_turn`` reply path; no tool needed.
* Cron-triggered messages — ``cron_manage`` already handles the
  recurring outbound case, and the cron tick itself produces the push.
* Mass-fan-out / broadcast — outside the v0.26 threat model. Build a
  separate operator-driven tool when that day comes.

Threat model
------------

* The text is bounded to 2 KiB — most IM platforms cap around 2–4 KiB
  and the conservative ceiling stops a runaway 50 KiB message that
  would fail at the wire anyway.
* Auto-resolving "current chat" mirrors :mod:`cron_manage`'s pattern:
  the chat the user is currently messaging from is the only sensible
  default. Sending to an arbitrary platform/user without explicit
  ``delivery_target_id`` would invite mis-deliveries.
* Sub-agents (:mod:`backend.agent.subagents`) cannot call this tool —
  their ``SUBAGENT_FORBIDDEN_TOOLS`` set includes ``send_message`` so a
  research fork cannot silently DM the operator with intermediate
  findings.
"""
from __future__ import annotations

from typing import Any, Optional, Union

from loguru import logger

from ...agent.context import current_turn_context
from ...db.models import DeliveryTarget as DeliveryTargetRow
from ...db.session import session_scope
from ...gateways.base import DeliveryTarget, OutgoingMessage
from ..base import Tool, ToolPermission, ToolResult

MAX_TEXT_CHARS = 2000
MAX_REASON_CHARS = 200


class SendMessageTool(Tool):
    name = "send_message"
    description = (
        "Send an unsolicited outbound message to an IM channel — use this"
        " when YOU want to volunteer information without the user asking"
        " (e.g. 'I noticed X', 'long task done', proactive reminder).\n\n"
        "Permission tier is **confirm**: every call triggers an IM yes/no"
        " because we are about to push text to a real human. Set ``reason``"
        " to a one-line summary so the operator can decide quickly.\n\n"
        "**Default routing**: ``to_current_chat=true`` sends back to the"
        " chat the user is currently messaging from — the same auto-resolve"
        " that ``cron_manage`` uses. Override with ``delivery_target_id``"
        " (an integer from ``GET /api/delivery-targets``) only when the"
        " user explicitly asked to push elsewhere.\n\n"
        "**Do NOT use** to reply to the current question — that is the"
        " normal assistant reply path and needs no tool. **Do NOT use** for"
        " recurring schedules — that is ``cron_manage``'s job.\n\n"
        "Hard caps: text ≤ 2000 chars, reason ≤ 200 chars."
    )
    permission = ToolPermission.CONFIRM
    is_read_only = False
    is_concurrency_safe = False
    # Sending an IM is recoverable (the human can be told to ignore the
    # last message); not flagged destructive in metadata, just confirm.
    is_destructive = False
    max_result_chars = 2_000
    search_hint = "send message proactive notify push outbound im chat"
    parameters_schema = {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": (
                    "The message body, 1–2000 chars. UTF-8 supported."
                    " ``print``-style formatting is fine; rich formatting"
                    " depends on the destination platform."
                ),
            },
            "to_current_chat": {
                "type": "boolean",
                "description": (
                    "Default true. When true, the message is routed to the"
                    " chat the user is currently messaging from"
                    " (auto-resolved from the active turn). When false,"
                    " ``delivery_target_id`` must be supplied."
                ),
            },
            "delivery_target_id": {
                "type": "integer",
                "description": (
                    "Existing delivery_target row id. Use only when the"
                    " user explicitly asks to push somewhere other than"
                    " the current chat."
                ),
            },
            "reason": {
                "type": "string",
                "description": (
                    "Optional one-line summary the operator sees in the"
                    " confirmation prompt — e.g. 'cron arxiv-daily failed"
                    " 3x'. Helps the operator decide yes/no quickly."
                    " Capped at 200 chars."
                ),
            },
        },
        "required": ["text"],
    }

    def __init__(self, gateway_manager: Any) -> None:
        # Typed loosely to avoid a circular import with backend.gateways.
        # The runtime dependency is just ``await dispatch(OutgoingMessage)``.
        self._gateway_manager = gateway_manager

    # ==================================================================

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        text = str(arguments.get("text") or "").strip()
        if not text:
            return ToolResult(
                ok=False, content="", error="text is required and must be non-empty",
            )
        if len(text) > MAX_TEXT_CHARS:
            return ToolResult(
                ok=False, content="",
                error=f"text exceeds {MAX_TEXT_CHARS} chars; split or summarise",
            )

        reason = str(arguments.get("reason") or "").strip()
        if len(reason) > MAX_REASON_CHARS:
            reason = reason[:MAX_REASON_CHARS] + "..."

        to_current = bool(arguments.get("to_current_chat", True))
        explicit_id = arguments.get("delivery_target_id")

        if explicit_id is not None and to_current:
            # Mutually exclusive — the LLM must pick one route.
            return ToolResult(
                ok=False, content="",
                error=(
                    "set either to_current_chat=true OR"
                    " delivery_target_id=<int>, not both"
                ),
            )
        if explicit_id is None and not to_current:
            return ToolResult(
                ok=False, content="",
                error=(
                    "to_current_chat=false but no delivery_target_id"
                    " supplied — pass an existing row id or set"
                    " to_current_chat=true."
                ),
            )

        target_or_err = self._resolve_target(
            to_current=to_current, explicit_id=explicit_id,
        )
        if isinstance(target_or_err, ToolResult):
            return target_or_err
        target, summary = target_or_err

        outgoing = OutgoingMessage(
            target=target,
            text=text,
            meta={"reason": reason} if reason else {},
        )
        try:
            await self._gateway_manager.dispatch(outgoing)
        except Exception as exc:  # noqa: BLE001 — surface, never crash the loop
            logger.warning(
                "send_message dispatch failed for {}: {}: {}",
                summary, type(exc).__name__, exc,
            )
            return ToolResult(
                ok=False, content="",
                error=f"dispatch failed: {type(exc).__name__}: {exc}",
            )

        body = f"sent {len(text)} chars to {summary}"
        if reason:
            body = f"{body} (reason: {reason})"
        return ToolResult(ok=True, content=body)

    # -- helpers -------------------------------------------------------

    def _resolve_target(
        self,
        *,
        to_current: bool,
        explicit_id: Optional[int],
    ) -> Union[tuple[DeliveryTarget, str], ToolResult]:
        """Return a ``(DeliveryTarget, summary)`` tuple or an error result.

        Mirrors :class:`backend.tools.builtins.cron_manage.CronManageTool`'s
        resolve helper but returns the live :class:`DeliveryTarget`
        dataclass (not a row id) because :meth:`GatewayManager.dispatch`
        wants the target object. We also auto-create a missing
        ``delivery_targets`` row for the current chat — same as
        ``cron_manage`` does — so the operator's audit table stays in
        sync with what the agent actually sent through.
        """
        if explicit_id is not None:
            try:
                target_id = int(explicit_id)
            except (TypeError, ValueError):
                return ToolResult(
                    ok=False, content="",
                    error="delivery_target_id must be an integer",
                )
            with session_scope() as session:
                row = session.get(DeliveryTargetRow, target_id)
                if row is None:
                    return ToolResult(
                        ok=False, content="",
                        error=f"delivery_target #{target_id} not found",
                    )
                if not row.enabled:
                    return ToolResult(
                        ok=False, content="",
                        error=f"delivery_target #{target_id} is disabled",
                    )
                target = DeliveryTarget(
                    platform=row.platform,
                    target_type=row.target_type,
                    target_id=row.target_id,
                    display_name=row.display_name or row.target_id,
                )
                summary = (
                    f"#{row.id} {row.platform}:{row.target_type}:{row.target_id}"
                )
                return target, summary

        # to_current=True path
        ctx = current_turn_context()
        if ctx is None or ctx.reply_target is None:
            return ToolResult(
                ok=False, content="",
                error=(
                    "to_current_chat=true but no active IM session"
                    " context. Call this tool from inside a real chat"
                    " turn or pass delivery_target_id explicitly."
                ),
            )
        # Mirror cron_manage: ensure the chat has a delivery_targets row
        # so operator listings stay consistent with what the agent sent.
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
                    "send_message auto-created delivery_target #{} for {}:{}:{}",
                    row.id, rt.platform, rt.target_type, rt.target_id,
                )
            elif not row.enabled:
                return ToolResult(
                    ok=False, content="",
                    error=(
                        f"current chat's delivery_target #{row.id} is"
                        " disabled — re-enable it via /api/delivery-targets"
                        " or pass a different delivery_target_id."
                    ),
                )
            summary = f"#{row.id} {rt.platform}:{rt.target_type}:{rt.target_id}"
        return rt, summary
