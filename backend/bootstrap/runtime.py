"""运行时集中装配。

该模块把原先散在 ``backend.app`` 中的 LLM、Skill、Memory、Tool、
MCP 与 Harness 创建过程集中到一个容器里。FastAPI 入口保留生命周期
和网关派发职责，AgentLoop 只保留执行职责。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from loguru import logger

from ..agent.loop import AgentLoop
from ..bootstrap.agent import build_agent, build_confirmation_store
from ..bootstrap.llm import build_llm, build_router_llm
from ..db.confirmations import ConfirmationStore
from ..db.session import SessionLocal
from ..harness import Harness, HarnessExecution, build_harness
from ..harness.accelerate.tool_memo import ToolMemo
from ..harness.observability.tracer import TraceRecorder
from ..harness.progress import ProgressEmitter
from ..llm import LLMClient
from ..skills.loader import SkillLoader
from ..tools import ToolRegistry
from ..tools.builtins import (
    CodeExecutionTool,
    DelegateTool,
    KnowledgeIngestTool,
    KnowledgeInspectTool,
    KnowledgeModeManageTool,
    MemoryManageTool,
    ReadFileTool,
    SendMessageTool,
    SkillManageTool,
    ToolSearchTool,
    WriteFileTool,
)
from ..tools.core_capabilities import (
    register_core_reminder_tools,
    register_core_web_query_tools,
)


@dataclass(slots=True)
class RuntimeContainer:
    """应用核心运行时对象集合。"""

    settings: Any
    redis_backend: Any
    llm_client: LLMClient
    router_llm: Any
    usage_store: Any
    skill_history_store: Any
    skill_guard: Any
    skill_loader: SkillLoader
    wiki_store: Any
    geo_store: Any
    memory_store: Any
    memory_manager: Any
    graph_extractor: Any
    tool_registry: ToolRegistry
    tool_execution: HarnessExecution
    confirmation_store: ConfirmationStore
    agent: AgentLoop
    mcp_manager: Any
    mcp_store: Any
    mcp_lifecycle: Any
    harness: Harness
    plugin_loader: Any = None
    tool_memo: Any = None
    tracer: Any = None
    progress: Any = None


async def build_runtime_container(
    *,
    app: Any,
    settings: Any,
    redis_backend: Any,
) -> RuntimeContainer:
    """创建核心运行时容器。"""
    llm_client = build_llm(settings)
    router_llm = build_router_llm(settings, llm_client)

    from ..skills.bootstrap import build_skill_subsystem

    skill_subsystem = build_skill_subsystem(
        settings,
        redis_backend=redis_backend,
        session_factory=SessionLocal,
    )

    from ..memory.bootstrap import build_memory_subsystem

    memory_store, memory_manager = build_memory_subsystem(
        settings,
        redis_backend=redis_backend,
    )
    graph_extractor = _build_graph_extractor(settings, llm_client)

    tool_registry = _build_tool_registry(
        settings=settings,
        usage_store=skill_subsystem.usage_store,
        skill_history_store=skill_subsystem.skill_history_store,
        skill_guard=skill_subsystem.skill_guard,
        skill_loader=skill_subsystem.skill_loader,
        memory_store=memory_store,
        memory_manager=memory_manager,
        graph_extractor=graph_extractor,
    )
    _register_travel_domain(
        app=app,
        settings=settings,
        redis_backend=redis_backend,
        tool_registry=tool_registry,
        wiki_store=skill_subsystem.wiki_store,
        geo_store=skill_subsystem.geo_store,
    )
    logger.info(
        "tool registry ready: {} tool(s) {} (travel_realtime bundle redis={})",
        len(tool_registry),
        tool_registry.counts_by_permission(),
        "on" if redis_backend is not None else "off",
    )

    tool_memo = ToolMemo()
    tracer = TraceRecorder()
    progress = ProgressEmitter()
    tool_execution = HarnessExecution(
        tool_registry,
        memo=tool_memo,
        tracer=tracer,
        progress=progress,
    )

    confirmation_store = build_confirmation_store(settings)
    agent = build_agent(
        settings,
        llm_client=llm_client,
        router_llm=router_llm,
        tool_registry=tool_registry,
        confirmation_store=confirmation_store,
        skill_loader=skill_subsystem.skill_loader,
        memory_manager=memory_manager,
        memory_store=memory_store,
        wiki_store=skill_subsystem.wiki_store,
        geo_store=skill_subsystem.geo_store,
        tool_execution=tool_execution,
        tracer=tracer,
    )
    tool_registry.register(DelegateTool(agent))

    from ..mcp.bootstrap import build_mcp_subsystem

    mcp_manager, mcp_store, mcp_lifecycle = await build_mcp_subsystem(
        settings,
        tool_registry=tool_registry,
        session_local=SessionLocal,
    )

    harness = build_harness(
        tool_registry=tool_registry,
        skill_loader=skill_subsystem.skill_loader,
        mcp_store=mcp_store,
        mcp_manager=mcp_manager,
        mcp_lifecycle=mcp_lifecycle,
        plugin_loader=None,
        tool_execution=tool_execution,
        tool_memo=tool_memo,
        tracer=tracer,
        progress=progress,
    )

    return RuntimeContainer(
        settings=settings,
        redis_backend=redis_backend,
        llm_client=llm_client,
        router_llm=router_llm,
        usage_store=skill_subsystem.usage_store,
        skill_history_store=skill_subsystem.skill_history_store,
        skill_guard=skill_subsystem.skill_guard,
        skill_loader=skill_subsystem.skill_loader,
        wiki_store=skill_subsystem.wiki_store,
        geo_store=skill_subsystem.geo_store,
        memory_store=memory_store,
        memory_manager=memory_manager,
        graph_extractor=graph_extractor,
        tool_registry=tool_registry,
        tool_execution=tool_execution,
        confirmation_store=confirmation_store,
        agent=agent,
        mcp_manager=mcp_manager,
        mcp_store=mcp_store,
        mcp_lifecycle=mcp_lifecycle,
        harness=harness,
        tool_memo=tool_memo,
        tracer=tracer,
        progress=progress,
    )


def attach_gateway_tools(runtime: RuntimeContainer, gateway_manager: Any) -> None:
    """注册依赖网关管理器的工具。"""
    runtime.tool_registry.register(SendMessageTool(gateway_manager=gateway_manager))


def load_runtime_plugins(runtime: RuntimeContainer) -> Any:
    """加载本地插件，并把插件加载器挂到 Harness。"""
    settings = runtime.settings
    plugin_loader = None
    if settings.plugins_enabled:
        from ..plugins.loader import PluginLoader

        plugin_loader = PluginLoader(
            plugins_dir=settings.plugins_dir or (settings.workspace_dir / "plugins"),
            tool_registry=runtime.tool_registry,
            workspace_dir=settings.workspace_dir,
            settings=settings,
            skill_guard=runtime.skill_guard,
            memory_manager=runtime.memory_manager,
        )
        try:
            plugin_loader.load_all()
        except Exception as exc:  # noqa: BLE001
            logger.exception("[plugins] loader crashed: {}", exc)
    runtime.plugin_loader = plugin_loader
    runtime.harness.plugin_loader = plugin_loader
    return plugin_loader


def bind_runtime_state(app: Any, runtime: RuntimeContainer) -> None:
    """把核心运行时对象写入 FastAPI state。"""
    app.state.redis_backend = runtime.redis_backend
    app.state.harness = runtime.harness
    app.state.agent = runtime.agent
    app.state.llm = runtime.llm_client
    app.state.graph_extractor = runtime.graph_extractor
    app.state.tool_registry = runtime.tool_registry
    app.state.tool_execution = runtime.tool_execution
    app.state.confirmation_store = runtime.confirmation_store
    app.state.skill_loader = runtime.skill_loader
    app.state.usage_store = runtime.usage_store
    app.state.memory_store = runtime.memory_store
    app.state.memory_manager = runtime.memory_manager
    app.state.wiki_store = runtime.wiki_store
    app.state.geo_store = runtime.geo_store
    app.state.failure_learner = runtime.agent._failure_learner
    app.state.mcp_manager = runtime.mcp_manager
    app.state.mcp_store = runtime.mcp_store
    app.state.mcp_lifecycle = runtime.mcp_lifecycle
    app.state.skill_history_store = runtime.skill_history_store
    app.state.plugin_loader = runtime.plugin_loader


def _build_graph_extractor(settings: Any, llm_client: LLMClient) -> Optional[Any]:
    """创建图谱抽取器。"""
    if not (settings.graph_llm_enabled and llm_client is not None):
        return None
    from ..graph.cache import LLMGraphCache
    from ..graph.llm_extractor import LLMGraphExtractor

    graph_cache_dir = settings.workspace_dir / "graph_cache"
    graph_cache_dir.mkdir(parents=True, exist_ok=True)
    return LLMGraphExtractor(
        llm=llm_client,
        cache=LLMGraphCache(graph_cache_dir),
        timeout_seconds=settings.graph_llm_timeout_seconds,
        max_content_chars=settings.graph_llm_max_chars,
    )


def _build_tool_registry(
    *,
    settings: Any,
    usage_store: Any,
    skill_history_store: Any,
    skill_guard: Any,
    skill_loader: SkillLoader,
    memory_store: Any,
    memory_manager: Any,
    graph_extractor: Any,
) -> ToolRegistry:
    """创建并填充核心工具注册表。"""
    tool_registry = ToolRegistry()
    tool_registry.register(ToolSearchTool(tool_registry))
    register_core_web_query_tools(tool_registry)
    tool_registry.register_many(
        [
            ReadFileTool(settings.workspace_dir, usage_store=usage_store),
            WriteFileTool(settings.workspace_dir),
            SkillManageTool(
                settings.workspace_dir / "skills",
                usage_store=usage_store,
                history_store=skill_history_store,
                guard=skill_guard,
            ),
            KnowledgeIngestTool(settings.workspace_dir, graph_extractor=graph_extractor),
            KnowledgeInspectTool(settings.workspace_dir),
            KnowledgeModeManageTool(settings.workspace_dir),
            MemoryManageTool(
                memory_store,
                memory_manager=memory_manager,
                max_fact_chars=settings.memory_max_fact_chars,
            ),
            CodeExecutionTool(settings.workspace_dir),
        ]
    )
    register_core_reminder_tools(tool_registry, skill_loader=skill_loader)
    return tool_registry


def _register_travel_domain(
    *,
    app: Any,
    settings: Any,
    redis_backend: Any,
    tool_registry: ToolRegistry,
    wiki_store: Any,
    geo_store: Any,
) -> None:
    """注册旅行领域工具和路由。"""
    from ..domains.travel import register_travel_domain

    base_url = (
        f"http://{settings.host}:{settings.port}"
        if settings.host != "0.0.0.0"
        else f"http://localhost:{settings.port}"
    )
    register_travel_domain(
        app=app,
        tool_registry=tool_registry,
        wiki_store=wiki_store,
        geo_store=geo_store,
        redis_backend=redis_backend,
        bundle_ttl_seconds=settings.redis_bundle_ttl_seconds,
        base_url=base_url,
    )
