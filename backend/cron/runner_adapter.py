"""Cron 到 Agent 的运行适配器。"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from loguru import logger

from ..db.models import CronJob, DeliveryTarget as DeliveryTargetRow
from ..db.session import session_scope
from ..gateways.base import DeliveryTarget, OutgoingMessage
from .deliverer import CronDeliverer
from .drafts import CronDraft, CronDraftStore


@dataclass(slots=True)
class CronRuntimeAdapter:
    """把 CronJob 转换为 Agent 推送任务。"""

    settings: Any
    agent: Any
    gateway_manager: Any
    draft_store: CronDraftStore
    deliverer: CronDeliverer

    async def run_job(self, job: CronJob) -> str:
        """执行单个 CronJob，并通过网关发送结果。"""
        dto = self._resolve_delivery_target(job)
        pre_script_output = await self._run_pre_script(job)
        content = await self.agent.generate_cron_message(
            job.instruction or "",
            job_name=job.name,
            skill_hint=job.skill_hint,
            pre_script_output=pre_script_output,
        )
        self._store_draft(job, dto, content)
        await self._dispatch_with_retry(job, dto, content)
        return "delivered"

    def _resolve_delivery_target(self, job: CronJob) -> DeliveryTarget:
        """读取 CronJob 对应的投递目标。"""
        if job.delivery_target_id is None:
            raise RuntimeError(
                f"cron job '{job.name}' has no delivery_target_id; cannot deliver"
            )

        with session_scope() as session:
            target = session.get(DeliveryTargetRow, job.delivery_target_id)
            if target is None:
                raise RuntimeError(
                    f"delivery target {job.delivery_target_id} not found for job '{job.name}'"
                )
            if not target.enabled:
                raise RuntimeError(
                    f"delivery target {target.display_name or target.target_id} is disabled"
                )
            return DeliveryTarget(
                platform=target.platform,
                target_type=target.target_type,
                target_id=target.target_id,
                display_name=target.display_name,
            )

    async def _run_pre_script(self, job: CronJob) -> Optional[str]:
        """执行 CronJob 的前置脚本，失败信息会进入最终推送文本。"""
        if not job.pre_script_path:
            return None
        from .pre_script import run_pre_script

        result = await run_pre_script(
            workspace_dir=self.settings.workspace_dir,
            script_path=job.pre_script_path,
            timeout_seconds=job.pre_script_timeout_seconds or 30,
            job_name=job.name,
        )
        if result.ok:
            logger.info(
                "pre_script for cron '{}' ok in {}ms ({} chars{})",
                job.name,
                result.duration_ms,
                len(result.stdout),
                ", truncated" if result.truncated else "",
            )
            return result.stdout

        logger.warning(
            "pre_script for cron '{}' failed: {} ({}ms)",
            job.name,
            result.error,
            result.duration_ms,
        )
        return (
            f"[pre_script failure] {result.error}\n"
            f"(stdout captured before failure was {len(result.stdout)} chars)"
        )

    def _store_draft(
        self,
        job: CronJob,
        dto: DeliveryTarget,
        content: str,
    ) -> None:
        """保存本次 Cron 生成的草稿。"""
        draft = CronDraft(
            job_id=job.id,
            job_name=job.name,
            planned_fire_at=datetime.utcnow(),
            generated_at=datetime.utcnow(),
            content=content,
            meta={
                "delivery_target": dto.describe(),
                "lead_minutes": getattr(job, "lead_minutes", 2),
            },
        )
        self.draft_store.put(draft)

    async def _dispatch_with_retry(
        self,
        job: CronJob,
        dto: DeliveryTarget,
        content: str,
    ) -> None:
        """按固定次数重试发送 Cron 推送。"""
        last_error: Optional[str] = None
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                if attempt > 1:
                    logger.warning(
                        "cron job '{}' retrying dispatch attempt {}/{}",
                        job.name,
                        attempt,
                        max_attempts,
                    )
                await self.gateway_manager.dispatch(OutgoingMessage(target=dto, text=content))
                self.deliverer.mark_delivered(job.id)
                logger.info(
                    "cron job '{}' delivered to {} ({} chars, llm={}, attempt={}/{})",
                    job.name,
                    dto.describe(),
                    len(content),
                    self.agent.llm_configured,
                    attempt,
                    max_attempts,
                )
                return
            except Exception as exc:  # noqa: BLE001
                last_error = f"dispatch failed: {type(exc).__name__}: {exc}"
                self.draft_store.mark_failed(job.id, last_error)
                logger.warning(
                    "cron job '{}' dispatch attempt {}/{} failed: {}",
                    job.name,
                    attempt,
                    max_attempts,
                    last_error,
                )
                if attempt < max_attempts:
                    await asyncio.sleep(2 ** (attempt - 1))
        raise RuntimeError(last_error or "dispatch failed")


def build_cron_runtime_adapter(
    *,
    settings: Any,
    agent: Any,
    gateway_manager: Any,
) -> CronRuntimeAdapter:
    """创建 Cron 运行适配器。"""
    draft_store = CronDraftStore()
    deliverer = CronDeliverer(gateway_manager.dispatch, draft_store)
    return CronRuntimeAdapter(
        settings=settings,
        agent=agent,
        gateway_manager=gateway_manager,
        draft_store=draft_store,
        deliverer=deliverer,
    )
