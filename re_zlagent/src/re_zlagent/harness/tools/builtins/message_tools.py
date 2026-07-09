"""Gateway message tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from ..base import Tool, ToolPermission, ToolResult
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
    """Boundary for sending tool-level outbound messages."""

    async def send(self, message: ToolOutgoingMessage) -> None:
        """Send the message."""


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

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        try:
            target = MessageTarget(
                platform=str(arguments.get("platform") or ""),
                target_type=str(arguments.get("target_type") or ""),
                target_id=str(arguments.get("target_id") or ""),
                display_name=str(arguments.get("display_name") or ""),
            )
            text = str(arguments.get("text") or "").strip()
            message = ToolOutgoingMessage(target=target, text=text)
        except ValueError as exc:
            return ToolResult.failure(
                str(exc),
                error_type=ToolErrorType.INVALID_INPUT,
                recoverable_by_model=True,
                recommended_next_action=RecommendedNextAction.RETRY,
                source=self.name,
            )

        try:
            await self._adapter.send(message)
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
                "bytes": len(text.encode("utf-8")),
            },
            evidence=[
                Evidence(
                    type="message_delivery",
                    ref=f"{target.platform}:{target.target_id}",
                    summary="outbound message accepted by gateway adapter",
                    metadata={"target_type": target.target_type},
                )
            ],
            side_effects=[
                SideEffect(
                    type="message",
                    target=f"{target.platform}:{target.target_id}",
                    risk="medium",
                    metadata={"target_type": target.target_type},
                )
            ],
            source=self.name,
        )
