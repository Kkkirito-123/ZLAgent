"""后台服务集中装配。

本模块收拢 Cron、技能维护、知识整理、每日复盘、图谱夜间任务等
长生命周期服务的创建、状态绑定、启动和关闭。FastAPI 入口只保留
服务生命周期调用。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from loguru import logger

from ..cron.scheduler import CronScheduler
from ..graph.nightly import NightlyGraphPipeline
from ..graph.scheduler import KnowledgeGraphScheduler
from ..graph.source import collect_graph_records
from ..memory.maintenance import MemoryMaintenance
from .runtime import RuntimeContainer


@dataclass(slots=True)
class RuntimeServices:
    """应用后台服务集合。"""

    cron_scheduler: CronScheduler
    graph_scheduler: KnowledgeGraphScheduler
    nightly_graph_pipeline: NightlyGraphPipeline
    skill_curator: Any
    curator_service: Any
    skill_knowledge_consolidator: Any
    skill_knowledge_service: Any
    daily_review_service: Any
    memory_maintenance: MemoryMaintenance


async def build_runtime_services(
    *,
    settings: Any,
    runtime: RuntimeContainer,
    gateway_manager: Any,
    cron_runner: Callable[[Any], Awaitable[str]],
    expire_pending_confirmations: Callable[[], Awaitable[None]],
) -> RuntimeServices:
    """创建后台服务集合。"""
    cron_scheduler = CronScheduler(
        runner=cron_runner,
        pre_tick_hook=expire_pending_confirmations,
    )

    from ..skills.curator import SkillCurator
    from ..skills.curator_service import SkillCuratorService

    skill_curator = SkillCurator(
        loader=runtime.skill_loader,
        usage_store=runtime.usage_store,
        stale_after_days=settings.skill_curator_stale_after_days,
        archive_after_days=settings.skill_curator_archive_after_days,
        dedupe_threshold=settings.skill_curator_dedupe_threshold,
    )
    curator_service = SkillCuratorService(
        skill_curator,
        warmup_seconds=settings.skill_curator_warmup_seconds,
        interval_seconds=settings.skill_curator_interval_seconds,
        enabled=settings.skill_curator_enabled,
    )

    from ..skills.consolidator import SkillKnowledgeConsolidator
    from ..skills.consolidator_service import SkillKnowledgeConsolidatorService

    knowledge_dir = settings.skill_knowledge_dir or (settings.workspace_dir / "knowledge")
    skill_knowledge_consolidator = SkillKnowledgeConsolidator(
        loader=runtime.skill_loader,
        knowledge_dir=knowledge_dir,
        workspace_dir=settings.workspace_dir,
        max_body_chars=settings.skill_knowledge_max_body_chars,
    )
    skill_knowledge_service = SkillKnowledgeConsolidatorService(
        skill_knowledge_consolidator,
        warmup_seconds=settings.skill_knowledge_warmup_seconds,
        interval_seconds=settings.skill_knowledge_interval_seconds,
        enabled=settings.skill_knowledge_enabled,
    )

    from ..skills.daily_review_service import DailyReviewService
    from ..skills.review_action_log import ReviewActionLog

    review_action_log = ReviewActionLog(workspace_dir=settings.workspace_dir)
    daily_review_service = DailyReviewService(
        memory_store=runtime.memory_store,
        skill_history_store=runtime.skill_history_store,
        usage_store=runtime.usage_store,
        agent=runtime.agent,
        warmup_seconds=settings.daily_review_warmup_seconds,
        interval_seconds=settings.daily_review_interval_seconds,
        lookback_seconds=settings.daily_review_lookback_seconds,
        max_iterations=settings.daily_review_max_iterations,
        enabled=settings.daily_review_enabled,
        gateway_manager=gateway_manager,
        push_target_id=settings.daily_review_push_target_id,
        action_log=review_action_log,
    )

    memory_maintenance = MemoryMaintenance(
        runtime.memory_store,
        runtime.memory_manager,
        skill_curator,
    )
    graph_scheduler = KnowledgeGraphScheduler(
        source_fn=collect_graph_records,
        export_dir=settings.workspace_dir / "graph",
        interval_seconds=settings.skill_knowledge_interval_seconds,
    )
    nightly_graph_pipeline = NightlyGraphPipeline(
        scheduler=graph_scheduler,
        export_dir=settings.workspace_dir / "graph",
        dispatch_fn=gateway_manager.dispatch,
        notify_target_id=settings.daily_review_push_target_id,
    )

    runtime.harness.daily_review_service = daily_review_service

    return RuntimeServices(
        cron_scheduler=cron_scheduler,
        graph_scheduler=graph_scheduler,
        nightly_graph_pipeline=nightly_graph_pipeline,
        skill_curator=skill_curator,
        curator_service=curator_service,
        skill_knowledge_consolidator=skill_knowledge_consolidator,
        skill_knowledge_service=skill_knowledge_service,
        daily_review_service=daily_review_service,
        memory_maintenance=memory_maintenance,
    )


def bind_service_state(app: Any, services: RuntimeServices) -> None:
    """把后台服务写入 FastAPI state。"""
    app.state.cron_scheduler = services.cron_scheduler
    app.state.graph_scheduler = services.graph_scheduler
    app.state.nightly_graph_pipeline = services.nightly_graph_pipeline
    app.state.skill_curator = services.skill_curator
    app.state.curator_service = services.curator_service
    app.state.skill_knowledge_consolidator = services.skill_knowledge_consolidator
    app.state.skill_knowledge_service = services.skill_knowledge_service
    app.state.daily_review_service = services.daily_review_service
    app.state.memory_maintenance = services.memory_maintenance


async def start_runtime_services(services: RuntimeServices) -> None:
    """按依赖顺序启动后台服务。"""
    await services.cron_scheduler.start()
    await services.graph_scheduler.start()
    await services.curator_service.start()
    await services.skill_knowledge_service.start()
    await services.daily_review_service.start()


async def stop_runtime_services(services: RuntimeServices) -> None:
    """按反向顺序关闭后台服务。"""
    await services.daily_review_service.stop()
    await services.skill_knowledge_service.stop()
    await services.curator_service.stop()
    await services.graph_scheduler.stop()
    await services.cron_scheduler.stop()


async def cancel_background_tasks(tasks: set[asyncio.Task[None]]) -> None:
    """取消入口层维护的异步任务集合。"""
    for task in tuple(tasks):
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    logger.debug("cancelled {} gateway background task(s)", len(tasks))
