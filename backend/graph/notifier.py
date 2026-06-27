from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..gateways.base import DeliveryTarget as RuntimeDeliveryTarget, OutgoingMessage


@dataclass
class GraphNotification:
    title: str
    summary: str
    target: RuntimeDeliveryTarget


class GraphNotifier:
    def __init__(self, dispatch_fn) -> None:
        self._dispatch = dispatch_fn

    async def send(self, notification: GraphNotification) -> None:
        text = f"{notification.title}\n\n{notification.summary}"
        await self._dispatch(OutgoingMessage(target=notification.target, text=text))
