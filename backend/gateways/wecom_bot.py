"""企业微信群机器人 outbound adapter.

A WeCom (企业微信) group bot is the simplest possible IM delivery: the admin
of a group adds a custom bot and receives a webhook URL of the form
``https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=<uuid>``. Sending a
message is a plain HTTP POST with a small JSON body; no OAuth, no tokens.

Each :class:`DeliveryTarget` whose ``platform == "wecom_bot"`` stores the full
webhook URL in ``target_id``. One gateway instance can serve many groups.

when ``OutgoingMessage.rich`` is populated, we render it through
:func:`backend.rich.renderers.render_wecom_bot_markdown` and send it as
``msgtype=markdown`` instead of plain text. The plain-text path is
preserved verbatim as the fallback (any failure renders, falls back).

Reference:
  https://developer.work.weixin.qq.com/document/path/91770
"""
from __future__ import annotations

import httpx
from loguru import logger

from .base import MessageGateway, MessageHandler, OutgoingMessage


class WeComBotGateway(MessageGateway):
    """Push-only gateway for 企业微信群机器人."""

    platform = "wecom_bot"
    kind = "webhook_push"

    def __init__(self, handler: MessageHandler, timeout_seconds: float = 10.0) -> None:
        super().__init__(handler)
        self._timeout = timeout_seconds

    async def start(self) -> None:
        logger.info("wecom_bot gateway ready (outbound only)")

    async def stop(self) -> None:
        logger.info("wecom_bot gateway stopped")

    async def send(self, message: OutgoingMessage) -> None:
        webhook_url = message.target.target_id
        if not webhook_url or not webhook_url.startswith(("http://", "https://")):
            raise ValueError(
                f"invalid wecom_bot webhook url: {webhook_url!r} (target_id must be the full URL)"
            )

        # when a RichMessage is attached, render it as
        # ``msgtype=markdown`` so the WeCom client renders bold,
        # tables, link cards, callouts natively. The renderer never
        # raises (it falls back to ``rich.fallback_text``); on the
        # very rare case it still produces an empty string we degrade
        # all the way to ``message.text`` so the user always sees
        # *something*.
        payload: dict
        body_chars: int
        if message.rich is not None and not message.rich.is_empty():
            try:
                # Local import keeps the gateways package importable
                # even when rich isn't installed (e.g. dev rigs).
                from ..rich.renderers import render_wecom_bot_markdown

                rendered = render_wecom_bot_markdown(message.rich)
            except Exception as exc:  # noqa: BLE001 - never break the send path
                logger.warning(
                    "[wecom_bot rich] render failed: {} — falling back to plain text",
                    exc,
                )
                rendered = ""

            if rendered.strip():
                # Official WeCom group bot schema:
                # https://developer.work.weixin.qq.com/document/path/91770
                # ``msgtype=markdown`` + a *top-level* ``markdown``
                # object (NOT a ``content`` object) carrying the
                # rendered string.
                payload = {"msgtype": "markdown", "markdown": {"content": rendered}}
                body_chars = len(rendered)
            else:
                payload = {"msgtype": "text", "text": {"content": message.text}}
                body_chars = len(message.text)
        else:
            payload = {"msgtype": "text", "text": {"content": message.text}}
            body_chars = len(message.text)

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(webhook_url, json=payload)
            resp.raise_for_status()
            data: dict = {}
            try:
                data = resp.json()
            except ValueError:
                pass
            errcode = int(data.get("errcode", 0))
            if errcode != 0:
                raise RuntimeError(f"wecom_bot api error: {data}")

        logger.info(
            "[wecom_bot send] target={} type={} len={} ok",
            message.target.describe(),
            payload["msgtype"],
            body_chars,
        )
