"""Dispatch application results through gateway adapters."""

from __future__ import annotations

from dataclasses import dataclass

from gateway import GatewayAdapter, IncomingMessage, OutgoingMessage

from .application import AgentApplication, ApplicationResult


@dataclass(frozen=True, slots=True)
class DispatchResult:
    """Result of handling and sending one inbound message."""

    application_result: ApplicationResult
    sent: bool
    error: str | None = None

    @property
    def outgoing(self) -> OutgoingMessage:
        return self.application_result.outgoing


class ApplicationDispatcher:
    """Thin app-to-gateway dispatcher."""

    def __init__(self, *, app: AgentApplication, gateway: GatewayAdapter) -> None:
        self._app = app
        self._gateway = gateway

    async def dispatch(self, message: IncomingMessage) -> DispatchResult:
        result = await self._app.handle_message(message)
        try:
            await self._gateway.send(result.outgoing)
        except Exception as exc:  # noqa: BLE001 - delivery failures are data
            return DispatchResult(
                application_result=result,
                sent=False,
                error=f"{type(exc).__name__}: {exc}",
            )
        return DispatchResult(application_result=result, sent=True)
