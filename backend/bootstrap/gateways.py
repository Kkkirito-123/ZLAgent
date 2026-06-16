"""入口网关装配。"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from ..gateways.base import IncomingMessage
from ..gateways.manager import GatewayManager
from ..gateways.webhook import WebhookGateway, build_router as build_webhook_router
from ..gateways.wecom_bot import WeComBotGateway
from ..gateways.weixin import WeixinGateway


def build_gateway_manager(
    *,
    app: Any,
    settings: Any,
    agent_handler: Callable[[IncomingMessage], Awaitable[None]],
) -> GatewayManager:
    """创建网关管理器并注册内置网关。"""
    manager = GatewayManager(
        agent_handler=agent_handler,
        per_user_concurrency=settings.per_user_concurrency,
    )

    webhook_gateway = WebhookGateway(handler=manager.handle_incoming)
    manager.register(webhook_gateway)
    app.include_router(build_webhook_router(webhook_gateway))

    wecom_bot_gateway = WeComBotGateway(handler=manager.handle_incoming)
    manager.register(wecom_bot_gateway)

    weixin_gateway = WeixinGateway(handler=manager.handle_incoming)
    manager.register(weixin_gateway)
    return manager
