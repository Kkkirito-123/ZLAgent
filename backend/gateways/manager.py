"""Gateway manager: registry + fan-out for channel adapters."""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Iterable

from loguru import logger

from .base import DeliveryTarget, IncomingMessage, MessageGateway, OutgoingMessage
from .status import GatewayStatus

AgentHandler = Callable[[IncomingMessage], Awaitable[None]]


class GatewayManager:
    """Owns all registered channel adapters and routes messages.

    The manager is intentionally thin: it only handles lifecycle and dispatch.
    Permission checks, pairing, rate limiting, and audit logging belong to
    higher-level middleware once introduced.

    Concurrency model (v0.32): ``handle_incoming`` enforces a per-user
    concurrency cap (default 3) using an :class:`asyncio.Semaphore` keyed by
    ``platform:user_id``. Different users hold independent semaphores and
    therefore run fully in parallel; the same user's messages queue FIFO
    once the cap is hit. This stops one slow tool call from blocking the
    same user's quick follow-up questions, while still bounding how many
    concurrent LLM turns one user can pin.
    """

    def __init__(
        self,
        agent_handler: AgentHandler,
        *,
        per_user_concurrency: int = 3,
    ) -> None:
        self._agent_handler = agent_handler
        self._gateways: dict[str, MessageGateway] = {}
        self._started: bool = False
        self._per_user_concurrency = max(1, int(per_user_concurrency or 1))
        self._user_semaphores: dict[str, asyncio.Semaphore] = {}
        # Lock only for the very rare race of two coroutines creating the
        # first semaphore for the same key. Acquired only on first sight
        # of a user, then never again on the hot path.
        self._semaphore_creation_lock = asyncio.Lock()

    # Registration ------------------------------------------------------------
    def register(self, gateway: MessageGateway) -> None:
        if gateway.platform in self._gateways:
            raise ValueError(f"gateway already registered for platform '{gateway.platform}'")
        self._gateways[gateway.platform] = gateway
        logger.info("gateway registered: {}", gateway.platform)

    def platforms(self) -> Iterable[str]:
        return tuple(self._gateways.keys())

    # Lifecycle ---------------------------------------------------------------
    async def start(self) -> None:
        if self._started:
            return
        if not self._gateways:
            logger.info("no gateways registered; running in headless mode")
            self._started = True
            return
        await asyncio.gather(*(gw.start() for gw in self._gateways.values()))
        self._started = True
        logger.info("gateway manager started with platforms: {}", list(self._gateways))

    async def stop(self) -> None:
        if not self._started:
            return
        results = await asyncio.gather(
            *(gw.stop() for gw in self._gateways.values()), return_exceptions=True
        )
        for platform, result in zip(self._gateways.keys(), results):
            if isinstance(result, Exception):
                logger.warning("gateway '{}' stop raised: {}", platform, result)
        self._started = False
        logger.info("gateway manager stopped")

    # Dispatch ----------------------------------------------------------------
    async def handle_incoming(self, message: IncomingMessage) -> None:
        """Entry point adapters call when they receive a message.

        Acquires the per-user concurrency slot (see class docstring) before
        delegating to the agent handler. Different users do not share a
        slot, so one slow user can't block another. The same user's 4th+
        in-flight message waits FIFO on the semaphore until a previous
        turn releases its slot.
        """
        logger.debug(
            "incoming message: platform={} channel={} user={} text={!r}",
            message.platform,
            message.channel_id,
            message.user_id,
            message.text[:160],
        )
        # bump inbound counter on the originating gateway when
        # registered. Anonymous platforms (smoke tests, future ad-hoc
        # gateways) silently bypass.
        gateway = self._gateways.get(message.platform)
        if gateway is not None:
            gateway.counters.record_inbound()
        sem = await self._get_user_semaphore(message)
        async with sem:
            await self._agent_handler(message)

    async def _get_user_semaphore(
        self, message: IncomingMessage
    ) -> asyncio.Semaphore:
        """Resolve the per-(platform, user) semaphore, creating it lazily.

        Uses double-checked locking only for first creation. The lock is
        not held on the hot path; once the dict has the key, lookups are
        a plain dict access.
        """
        key = self._user_key(message)
        sem = self._user_semaphores.get(key)
        if sem is not None:
            return sem
        async with self._semaphore_creation_lock:
            sem = self._user_semaphores.get(key)
            if sem is None:
                sem = asyncio.Semaphore(self._per_user_concurrency)
                self._user_semaphores[key] = sem
        return sem

    @staticmethod
    def _user_key(message: IncomingMessage) -> str:
        return f"{message.platform}:{message.user_id or '_anonymous_'}"

    @property
    def per_user_concurrency(self) -> int:
        return self._per_user_concurrency

    def user_semaphore_keys(self) -> tuple[str, ...]:
        """Test-friendly view of which (platform, user) pairs have been seen."""
        return tuple(self._user_semaphores.keys())

    async def dispatch(self, message: OutgoingMessage) -> None:
        """Deliver ``message`` through the gateway owning its platform."""
        gateway = self._gateways.get(message.target.platform)
        if gateway is None:
            logger.warning(
                "no gateway for platform '{}'; dropping message", message.target.platform
            )
            return
        try:
            await gateway.send(message)
        except Exception as exc:  # noqa: BLE001
            gateway.counters.record_failure(f"{type(exc).__name__}: {exc}")
            raise
        gateway.counters.record_outbound()

    async def broadcast(
        self, text: str, targets: Iterable[DeliveryTarget]
    ) -> None:
        """Convenience helper to send the same text to many targets."""
        for target in targets:
            await self.dispatch(OutgoingMessage(target=target, text=text))

    # v0.22 status surface ---------------------------------------------------
    def gateway_statuses(self) -> list[GatewayStatus]:
        """Return a snapshot of every registered gateway's status."""
        out: list[GatewayStatus] = []
        for platform, gw in self._gateways.items():
            try:
                configured = bool(gw.is_configured())
            except Exception as exc:  # noqa: BLE001 - never break the listing
                logger.debug("gateway '{}' is_configured raised: {}", platform, exc)
                configured = False
            try:
                extra = gw.status_extra() or {}
            except Exception as exc:  # noqa: BLE001
                logger.debug("gateway '{}' status_extra raised: {}", platform, exc)
                extra = {}
            out.append(
                GatewayStatus(
                    name=platform,
                    kind=gw.kind or platform,
                    configured=configured,
                    counters=gw.counters,
                    extra=extra,
                )
            )
        return out

    def get(self, platform: str) -> MessageGateway | None:
        """Return the registered adapter for ``platform`` or None."""
        return self._gateways.get(platform)
