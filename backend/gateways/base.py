"""Abstract base classes and dataclasses for channel adapters.

Every messaging channel (WeChat/WeCom, Feishu, Telegram, email, webhook,
...) plugs into the agent through a single ``MessageGateway`` interface.
The agent core never imports a specific channel; it only sees normalized
``IncomingMessage`` events and ``DeliveryTarget`` objects.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Iterable, Optional

from .status import GatewayCounters

if TYPE_CHECKING:
    # RichMessage carries the structured payload that
    # rich-capable gateways (wecom_bot markdown, future feishu)
    # render natively. Imported under TYPE_CHECKING so the gateways
    # package can still load when ``backend.rich`` isn't on the
    # path (e.g. trimmed deployments / smoke tests that exercise
    # only the gateway abstraction).
    from ..rich.schema import RichMessage  # noqa: F401


@dataclass(slots=True)
class Attachment:
    """Minimal attachment reference.

    Concrete adapters may keep the raw URL/file id and fetch content lazily;
    the core agent only sees the normalized mime type and display name.
    """

    kind: str  # image | audio | video | file | link
    name: str = ""
    mime_type: str = ""
    url: Optional[str] = None
    local_path: Optional[str] = None
    size_bytes: Optional[int] = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class DeliveryTarget:
    """Where an outgoing message should be delivered.

    A DeliveryTarget is platform-qualified, so one agent can deliver into
    multiple WeChat groups, Feishu chats, or emails without any ambiguity.
    """

    platform: str
    target_type: str  # user | group | channel | email
    target_id: str
    display_name: str = ""

    def describe(self) -> str:
        return f"{self.platform}:{self.target_type}:{self.target_id}"


@dataclass(slots=True)
class IncomingMessage:
    """A message the agent just received from any channel."""

    platform: str
    channel_id: str  # group/chat/email-thread identifier
    user_id: str
    message_id: str
    text: str = ""
    attachments: list[Attachment] = field(default_factory=list)
    timestamp: datetime = field(default_factory=datetime.utcnow)
    reply_target: Optional[DeliveryTarget] = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class OutgoingMessage:
    """A message the agent wants to send to a channel.

    v0.38 added the optional ``rich`` payload. When set, a
    rich-capable gateway will render it through its native markdown /
    card primitives and ignore ``text``. Gateways that do NOT support
    rich content fall back to ``text`` verbatim — so the agent layer
    must always populate ``text`` with a plain-text fallback (the
    rich content parser does this automatically from the surrounding
    LLM narrative). This is a deliberate "deliver something legible
    on every platform" contract: a partial / rich-only message is a
    bug.
    """

    target: DeliveryTarget
    text: str = ""
    attachments: list[Attachment] = field(default_factory=list)
    reply_to_message_id: Optional[str] = None
    meta: dict[str, Any] = field(default_factory=dict)
    # structured rich content. ``Optional["RichMessage"]`` is
    # forward-quoted so importers don't pay the cost when they don't
    # need it; see TYPE_CHECKING import at the top of this file.
    rich: Optional["RichMessage"] = None


MessageHandler = Callable[[IncomingMessage], Awaitable[None]]


class MessageGateway(abc.ABC):
    """Base class every channel adapter inherits from.

    Concrete adapters:
      * WeComGateway   -> 企业微信应用/机器人
      * FeishuGateway  -> Feishu/Lark
      * EmailGateway   -> SMTP + IMAP
      * TelegramGateway
      * WebhookGateway -> generic HTTP receiver

    They must never reach into the agent loop directly. Incoming messages go
    through ``handler`` injected by the manager; outgoing messages arrive via
    ``send`` from the dispatcher.
    """

    #: Short platform identifier, e.g. "weixin" / "wecom" / "feishu" / "email".
    platform: str = "base"
    #: Coarse adapter kind for /api/gateways listing — defaults to platform.
    kind: str = "base"

    def __init__(self, handler: MessageHandler) -> None:
        self._handler = handler
        # every gateway tracks its own runtime counters. Wired up
        # by GatewayManager so adapters don't need to remember to call.
        self.counters: GatewayCounters = GatewayCounters()

    @abc.abstractmethod
    async def start(self) -> None:
        """Start the adapter (long polling, websocket, webhook registration)."""

    @abc.abstractmethod
    async def stop(self) -> None:
        """Stop the adapter and release any resources."""

    @abc.abstractmethod
    async def send(self, message: OutgoingMessage) -> None:
        """Deliver ``message`` to its ``target``. May raise on fatal errors."""

    # Convenience -------------------------------------------------------------
    async def emit(self, message: IncomingMessage) -> None:
        """Adapters call this to hand an incoming message to the agent core."""
        await self._handler(message)

    def supports(self, target: DeliveryTarget) -> bool:
        """Return True if this adapter can deliver to ``target``."""
        return target.platform == self.platform

    def targets(self) -> Iterable[DeliveryTarget]:
        """Optional: list known delivery targets (groups/users) for UI help."""
        return ()

    def is_configured(self) -> bool:
        """does this adapter have what it needs to actually run?

        Default to True; concrete adapters (e.g. weixin, wecom_bot) override
        to reflect whether their secrets / endpoint pairings are present.
        """
        return True

    def status_extra(self) -> dict[str, Any]:
        """Optional adapter-specific status fields rendered under ``extra``."""
        return {}
