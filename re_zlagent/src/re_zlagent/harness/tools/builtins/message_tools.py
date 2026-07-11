"""Gateway message tools."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Protocol

from ..base import Tool, ToolExecutionContext, ToolPermission, ToolResult
from ..metadata import Evidence, RecommendedNextAction, SideEffect, ToolErrorType


@dataclass(frozen=True, slots=True)
class MessageTarget:
    """Harness-level outbound message target."""

    platform: str
    target_type: str
    target_id: str
    display_name: str = ""

    def __post_init__(self) -> None:
        if not self.platform.strip():
            raise ValueError("target.platform must be non-empty")
        if not self.target_type.strip():
            raise ValueError("target.target_type must be non-empty")
        if not self.target_id.strip():
            raise ValueError("target.target_id must be non-empty")


@dataclass(frozen=True, slots=True)
class ToolOutgoingMessage:
    """Harness-level outbound message payload."""

    target: MessageTarget
    text: str

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("message.text must be non-empty")


class MessageSender(Protocol):
    """Idempotent boundary for sending tool-level outbound messages."""

    async def send(
        self,
        message: ToolOutgoingMessage,
        *,
        idempotency_key: str,
    ) -> None:
        """Send once for a stable idempotency key, deduplicating retries."""


class SendMessageTool(Tool):
    """Send an outbound message through a gateway adapter."""

    name = "send_message"
    description = (
        "Send a text message to a normalized delivery target. This is an "
        "externally visible action and requires confirmation."
    )
    permission = ToolPermission.CONFIRM
    is_read_only = False
    is_concurrency_safe = False
    is_destructive = False
    side_effects = ("message",)
    outbox_required = True
    side_effect_retry_safe = True
    input_schema = {
        "type": "object",
        "properties": {
            "platform": {"type": "string"},
            "target_type": {"type": "string"},
            "target_id": {"type": "string"},
            "text": {"type": "string"},
            "display_name": {"type": "string"},
        },
        "required": ["platform", "target_type", "target_id", "text"],
    }

    def __init__(self, adapter: MessageSender) -> None:
        self._adapter = adapter

    def plan_side_effects(
        self,
        arguments: dict[str, Any],
    ) -> tuple[SideEffect, ...]:
        target, _ = self._parse_message(arguments)
        return (self._side_effect(target),)

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        canonical = json.dumps(
            arguments,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            default=str,
        ).encode("utf-8")
        direct_key = f"direct:send_message:{hashlib.sha256(canonical).hexdigest()}"
        return await self._execute(arguments, idempotency_key=direct_key)

    async def execute_with_context(
        self,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        idempotency_key = (
            context.side_effect_keys[0]
            if context.side_effect_keys
            else context.idempotency_key
        )
        return await self._execute(arguments, idempotency_key=idempotency_key)

    async def _execute(
        self,
        arguments: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> ToolResult:
        try:
            target, message = self._parse_message(arguments)
        except ValueError as exc:
            return ToolResult.failure(
                str(exc),
                error_type=ToolErrorType.INVALID_INPUT,
                recoverable_by_model=True,
                recommended_next_action=RecommendedNextAction.RETRY,
                source=self.name,
            )

        try:
            await self._adapter.send(
                message,
                idempotency_key=idempotency_key,
            )
        except Exception as exc:  # noqa: BLE001 - gateway failures are data
            return ToolResult.failure(
                f"{type(exc).__name__}: {exc}",
                error_type=ToolErrorType.EXTERNAL_UNAVAILABLE,
                recoverable_by_model=True,
                recommended_next_action=RecommendedNextAction.RETRY,
                source=self.name,
            )

        return ToolResult.success(
            f"Sent message to {target.platform}:{target.target_id}",
            raw={
                "platform": target.platform,
                "target_type": target.target_type,
                "target_id": target.target_id,
                "bytes": len(message.text.encode("utf-8")),
                "idempotency_key": idempotency_key,
            },
            evidence=[
                Evidence(
                    type="message_delivery",
                    ref=f"{target.platform}:{target.target_id}",
                    summary="outbound message accepted by gateway adapter",
                    metadata={
                        "target_type": target.target_type,
                        "idempotency_key": idempotency_key,
                    },
                )
            ],
            side_effects=[self._side_effect(target)],
            source=self.name,
        )

    @staticmethod
    def _parse_message(
        arguments: dict[str, Any],
    ) -> tuple[MessageTarget, ToolOutgoingMessage]:
        target = MessageTarget(
            platform=str(arguments.get("platform") or ""),
            target_type=str(arguments.get("target_type") or ""),
            target_id=str(arguments.get("target_id") or ""),
            display_name=str(arguments.get("display_name") or ""),
        )
        text = str(arguments.get("text") or "").strip()
        return target, ToolOutgoingMessage(target=target, text=text)

    @staticmethod
    def _side_effect(target: MessageTarget) -> SideEffect:
        return SideEffect(
            type="message",
            target=f"{target.platform}:{target.target_id}",
            risk="medium",
            metadata={"target_type": target.target_type},
        )
