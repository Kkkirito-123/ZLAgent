"""IM 消息到 Agent turn 的派发器。"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

from loguru import logger

from ..agent.routing import should_ack_first
from ..gateways.base import DeliveryTarget, IncomingMessage, OutgoingMessage
from ..harness.progress import (
    bind_sink as _progress_bind_sink,
    current_turn_has_emitted,
    unbind_sink as _progress_unbind_sink,
)
from .runtime import RuntimeContainer


class GatewayTurnDispatcher:
    """处理网关入站消息并驱动 Agent turn。"""

    def __init__(self, runtime: RuntimeContainer) -> None:
        """保存运行时对象，网关管理器稍后绑定。"""
        self._runtime = runtime
        self._manager: Any = None
        self._ack_background_tasks: set[asyncio.Task[None]] = set()

    def bind_gateway_manager(self, manager: Any) -> None:
        """绑定网关管理器。"""
        self._manager = manager

    async def route_message(self, message: IncomingMessage) -> None:
        """处理单条入站消息。"""
        self._ensure_manager()
        text = (message.text or "").strip()
        if text.startswith("/"):
            handled = await self._try_harness_command(message, text)
            if handled:
                return

        if self._should_ack_first(message):
            await self._send_ack_and_continue(message)
            return
        await self._run_agent_and_dispatch(message, already_acked=False)

    async def stop(self) -> None:
        """取消仍在后台处理的 ack 后续任务。"""
        for task in tuple(self._ack_background_tasks):
            task.cancel()
        if self._ack_background_tasks:
            await asyncio.gather(*self._ack_background_tasks, return_exceptions=True)

    async def _try_harness_command(self, message: IncomingMessage, text: str) -> bool:
        """尝试处理 Harness 控制命令。"""
        try:
            harness_reply = await self._runtime.harness.handle_command(text)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[harness] command failed for {!r}: {}", text, exc)
            harness_reply = None
        if harness_reply is None:
            return False
        await self._manager.dispatch(
            OutgoingMessage(target=message.reply_target, text=harness_reply)
        )
        return True

    def _should_ack_first(self, message: IncomingMessage) -> bool:
        """判断是否先发送受理提示。"""
        return (
            should_ack_first(message)
            and message.user_id
            and self._runtime.confirmation_store.find_pending_for(
                platform=message.platform,
                user_id=message.user_id,
            ) is None
        )

    async def _send_ack_and_continue(self, message: IncomingMessage) -> None:
        """先发受理提示，再把 Agent turn 放入后台任务。"""
        try:
            await self._manager.dispatch(
                OutgoingMessage(
                    target=message.reply_target,
                    text="好的，我来处理。",
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "ack-first dispatch failed for platform={} user={}: {}",
                message.platform,
                message.user_id,
                exc,
            )
        task = asyncio.create_task(
            self._run_agent_and_dispatch(message, already_acked=True)
        )
        self._ack_background_tasks.add(task)
        task.add_done_callback(self._ack_background_tasks.discard)

    async def _run_agent_and_dispatch(
        self,
        message: IncomingMessage,
        *,
        already_acked: bool = False,
    ) -> None:
        """运行 Agent turn，并把结果发回原网关。"""
        progress_token = None
        ack_task: Optional[asyncio.Task[None]] = None
        emitter = getattr(self._runtime.harness, "progress", None)
        if message.reply_target is not None and emitter is not None:
            target = message.reply_target

            async def _sink(text: str) -> None:
                await self._progress_send_to_gateway(target, text)

            progress_token = _progress_bind_sink(
                _sink,
                cooldown_seconds=emitter.default_cooldown_seconds,
            )
            if already_acked:
                emitter.disable_for_current_turn()
                from ..harness.progress.emitter import _TURN_CTX as _PROGRESS_CTX

                ctx = _PROGRESS_CTX.get()
                if ctx is not None:
                    ctx.enabled = True
            else:
                ack_task = asyncio.create_task(
                    self._delayed_thinking_ack(3.0, target)
                )

        try:
            reply = await self._runtime.agent.run_turn(
                message,
                dispatch_fn=self._manager.dispatch,
            )
            if reply is not None:
                await self._manager.dispatch(reply)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "background agent turn failed: platform={} user={} error={}",
                message.platform,
                message.user_id,
                exc,
            )
        finally:
            if ack_task is not None:
                ack_task.cancel()
                try:
                    await ack_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            if progress_token is not None:
                _progress_unbind_sink(progress_token)

    async def _progress_send_to_gateway(
        self,
        target: DeliveryTarget,
        text: str,
    ) -> None:
        """发送工具进度提示。"""
        try:
            await self._manager.dispatch(OutgoingMessage(target=target, text=text))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[harness.progress] sink dispatch failed: {} (target={})",
                exc,
                target,
            )

    async def _delayed_thinking_ack(
        self,
        delay_seconds: float,
        target: DeliveryTarget,
    ) -> None:
        """延迟发送思考提示。"""
        try:
            await asyncio.sleep(delay_seconds)
        except asyncio.CancelledError:
            return
        if current_turn_has_emitted():
            return
        emitter = getattr(self._runtime.harness, "progress", None)
        if emitter is None:
            return
        await emitter.phase_changed("starting")

    def _ensure_manager(self) -> None:
        """确保网关管理器已经绑定。"""
        if self._manager is None:
            raise RuntimeError("GatewayTurnDispatcher has no GatewayManager bound")
