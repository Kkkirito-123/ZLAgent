"""Application bootstrap helpers."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from re_zlagent.harness import HarnessFacade, build_harness_facade
from re_zlagent.harness.agent import (
    AgentOrchestrator,
    AgentPlanner,
    GeneralAgent,
    JsonIntentRouter,
    JsonPlanPlanner,
)
from re_zlagent.harness.conversation import (
    ConversationManager,
    ConversationPolicy,
    ConversationStore,
    InMemoryConversationStore,
    ModelConversationSummarizer,
    SqliteConversationStore,
)
from re_zlagent.harness.memory import (
    InMemoryMemoryStore,
    MemoryManager,
    MemoryStore,
    SqliteMemoryStore,
)
from re_zlagent.harness.mcp import LocalMcpClient, create_mcp_tools, load_mcp_config
from re_zlagent.harness.model import ModelClient
from re_zlagent.harness.progress import TaskProgressReader
from re_zlagent.harness.runtime import HarnessRuntime
from re_zlagent.harness.skills import (
    FileSystemSkillLoader,
    GitHubApiClient,
    GitHubSkillInstaller,
    LocalSkillInstaller,
    SkillSelector,
)
from re_zlagent.harness.storage import (
    InMemoryLongTaskStore,
    InMemoryTaskStore,
    LongTaskStore,
    SqliteLongTaskStore,
    SqliteTaskStore,
    TaskStore,
)
from re_zlagent.harness.tools import ToolRegistry
from re_zlagent.harness.tools.builtins import (
    InstallGitHubSkillTool,
    InstallSkillTool,
    create_file_tools,
)

from .application import AgentApplication
from .operator import ApprovalService, OperatorService


@dataclass(frozen=True, slots=True)
class ApplicationBootstrapConfig:
    """Configuration for assembling an AgentApplication container."""

    workspace_dir: Path | None = None
    sqlite_path: Path | None = None
    register_file_tools: bool = True
    skill_import_dir: Path | None = None
    skills_dir: Path | None = None
    mcp_config_path: Path | None = None
    allow_auto_task_execution: bool = False
    allow_github_skill_install: bool = False
    conversation_raw_retention_rounds: int = 32
    conversation_target_recent_rounds: int = 8
    conversation_threshold_extra_rounds: int = 4
    conversation_max_recent_tokens: int = 3_000
    conversation_max_summary_tokens: int = 800

    def __post_init__(self) -> None:
        if self.workspace_dir is not None:
            object.__setattr__(self, "workspace_dir", Path(self.workspace_dir))
        if self.sqlite_path is not None:
            object.__setattr__(self, "sqlite_path", Path(self.sqlite_path))
        if self.skill_import_dir is not None:
            object.__setattr__(self, "skill_import_dir", Path(self.skill_import_dir))
        if self.skills_dir is not None:
            object.__setattr__(self, "skills_dir", Path(self.skills_dir))
        if self.mcp_config_path is not None:
            object.__setattr__(self, "mcp_config_path", Path(self.mcp_config_path))
        if self.skill_import_dir is not None and self.skills_dir is None:
            raise ValueError(
                "skill_import_dir requires skills_dir"
            )
        if self.allow_github_skill_install and self.skills_dir is None:
            raise ValueError(
                "allow_github_skill_install requires a managed Skill directory"
            )
        ConversationPolicy(
            raw_retention_rounds=self.conversation_raw_retention_rounds,
            target_recent_rounds=self.conversation_target_recent_rounds,
            threshold_extra_rounds=self.conversation_threshold_extra_rounds,
            max_recent_tokens=self.conversation_max_recent_tokens,
            max_summary_tokens=self.conversation_max_summary_tokens,
        )


@dataclass(frozen=True, slots=True)
class ApplicationContainer:
    """Concrete assembled application components."""

    app: AgentApplication
    general_agent: GeneralAgent
    facade: HarnessFacade
    orchestrator: AgentOrchestrator
    runtime: HarnessRuntime
    store: TaskStore
    long_task_store: LongTaskStore
    tools: ToolRegistry
    planner: AgentPlanner
    operator: OperatorService
    approvals: ApprovalService
    progress_reader: TaskProgressReader
    memory_store: MemoryStore
    memory_manager: MemoryManager
    conversation_store: ConversationStore
    conversation_manager: ConversationManager
    mcp_client: LocalMcpClient | None

    def close(self) -> None:
        """Close owned resources when adapters expose a close method."""

        if self.mcp_client is not None:
            self.mcp_client.close()
        _close_resource(self.conversation_store)
        _close_resource(self.memory_store)
        close = getattr(self.store, "close", None)
        if callable(close):
            close()
        long_task_close = getattr(self.long_task_store, "close", None)
        if callable(long_task_close):
            long_task_close()


@dataclass(frozen=True, slots=True)
class ApplicationRuntimeContainer:
    """Runtime services that do not require a planner or model client."""

    facade: HarnessFacade
    runtime: HarnessRuntime
    store: TaskStore
    long_task_store: LongTaskStore
    tools: ToolRegistry
    operator: OperatorService
    approvals: ApprovalService
    progress_reader: TaskProgressReader
    memory_store: MemoryStore
    memory_manager: MemoryManager
    mcp_client: LocalMcpClient | None

    def close(self) -> None:
        """Close owned resources when adapters expose a close method."""

        if self.mcp_client is not None:
            self.mcp_client.close()
        _close_resource(self.memory_store)
        close = getattr(self.store, "close", None)
        if callable(close):
            close()
        long_task_close = getattr(self.long_task_store, "close", None)
        if callable(long_task_close):
            long_task_close()


def build_application_container(
    *,
    planner: AgentPlanner | None = None,
    model: ModelClient | None = None,
    response_model: ModelClient | None = None,
    store: TaskStore | None = None,
    long_task_store: LongTaskStore | None = None,
    memory_store: MemoryStore | None = None,
    conversation_store: ConversationStore | None = None,
    tool_registry: ToolRegistry | None = None,
    config: ApplicationBootstrapConfig | None = None,
    environ: Mapping[str, str] | None = None,
) -> ApplicationContainer:
    """Build the minimal re_zlagent application graph."""

    cfg = config or ApplicationBootstrapConfig()
    _validate_planner_inputs(planner=planner, model=model)
    services = build_application_runtime(
        store=store,
        long_task_store=long_task_store,
        memory_store=memory_store,
        tool_registry=tool_registry,
        config=cfg,
        environ=environ,
    )
    resolved_conversation_store = conversation_store or _build_conversation_store(
        cfg
    )
    try:
        resolved_planner = _resolve_planner(
            planner=planner,
            model=model,
            tools=services.tools,
        )
    except Exception:
        _close_resource(resolved_conversation_store)
        services.close()
        raise
    orchestrator = AgentOrchestrator(
        planner=resolved_planner,
        runtime=services.runtime,
    )
    conversation_policy = ConversationPolicy(
        raw_retention_rounds=cfg.conversation_raw_retention_rounds,
        target_recent_rounds=cfg.conversation_target_recent_rounds,
        threshold_extra_rounds=cfg.conversation_threshold_extra_rounds,
        max_recent_tokens=cfg.conversation_max_recent_tokens,
        max_summary_tokens=cfg.conversation_max_summary_tokens,
    )
    conversation_model = response_model if response_model is not None else model
    conversation_manager = ConversationManager(
        resolved_conversation_store,
        summarizer=(
            ModelConversationSummarizer(
                conversation_model,
                max_summary_tokens=conversation_policy.max_summary_tokens,
            )
            if conversation_model is not None
            else None
        ),
        policy=conversation_policy,
    )
    general_agent = GeneralAgent(
        orchestrator=orchestrator,
        response_model=response_model if response_model is not None else model,
        intent_router=JsonIntentRouter(model) if model is not None else None,
        memory_manager=services.memory_manager,
        conversation_manager=conversation_manager,
        skill_selector=(
            SkillSelector(services.facade.skill_loader)
            if services.facade.skill_loader is not None
            else None
        ),
        allow_auto_task_execution=cfg.allow_auto_task_execution,
    )
    app = AgentApplication(orchestrator=orchestrator)
    return ApplicationContainer(
        app=app,
        general_agent=general_agent,
        facade=services.facade,
        orchestrator=orchestrator,
        runtime=services.runtime,
        store=services.store,
        long_task_store=services.long_task_store,
        tools=services.tools,
        planner=resolved_planner,
        operator=services.operator,
        approvals=services.approvals,
        progress_reader=services.progress_reader,
        memory_store=services.memory_store,
        memory_manager=services.memory_manager,
        conversation_store=resolved_conversation_store,
        conversation_manager=conversation_manager,
        mcp_client=services.mcp_client,
    )


def build_application_runtime(
    *,
    store: TaskStore | None = None,
    long_task_store: LongTaskStore | None = None,
    memory_store: MemoryStore | None = None,
    tool_registry: ToolRegistry | None = None,
    config: ApplicationBootstrapConfig | None = None,
    environ: Mapping[str, str] | None = None,
) -> ApplicationRuntimeContainer:
    """Build runtime/operator services without requiring model configuration."""

    cfg = config or ApplicationBootstrapConfig()
    resolved_store = store or _build_store(cfg)
    resolved_long_task_store = long_task_store or _build_long_task_store(cfg)
    resolved_memory_store = memory_store or _build_memory_store(cfg)
    tools = tool_registry or ToolRegistry()
    mcp_client: LocalMcpClient | None = None
    try:
        if cfg.register_file_tools and cfg.workspace_dir is not None:
            for file_tool in create_file_tools(cfg.workspace_dir):
                tools.register(file_tool)
        skill_loader: FileSystemSkillLoader | None = None
        if cfg.skills_dir is not None:
            skill_loader = FileSystemSkillLoader(cfg.skills_dir)
            skill_loader.load()
        if cfg.skill_import_dir is not None and cfg.skills_dir is not None:
            installer = LocalSkillInstaller(
                cfg.skill_import_dir,
                cfg.skills_dir,
                loader=skill_loader,
            )
            tools.register(InstallSkillTool(installer))
        if cfg.allow_github_skill_install and cfg.skills_dir is not None:
            environment = os.environ if environ is None else environ
            github_installer = GitHubSkillInstaller(
                cfg.skills_dir,
                loader=skill_loader,
                client=GitHubApiClient(token=environment.get("GITHUB_TOKEN")),
            )
            tools.register(InstallGitHubSkillTool(github_installer))
        if cfg.mcp_config_path is not None:
            mcp_client = LocalMcpClient(
                load_mcp_config(cfg.mcp_config_path),
                environ=environ,
            )
            mcp_client.start()
            for mcp_tool in create_mcp_tools(mcp_client):
                tools.register(mcp_tool)
    except Exception:
        if mcp_client is not None:
            mcp_client.close()
        _close_resource(resolved_store)
        _close_resource(resolved_long_task_store)
        _close_resource(resolved_memory_store)
        raise

    runtime = HarnessRuntime(
        store=resolved_store,
        tools=tools,
        long_task_store=resolved_long_task_store,
    )
    operator = OperatorService(resolved_store)
    approvals = ApprovalService(runtime)
    progress_reader = TaskProgressReader(resolved_store)
    memory_manager = MemoryManager(resolved_memory_store)
    facade = build_harness_facade(
        tool_registry=tools,
        skill_loader=skill_loader,
        task_store=resolved_store,
        progress_reader=progress_reader,
    )
    return ApplicationRuntimeContainer(
        facade=facade,
        runtime=runtime,
        store=resolved_store,
        long_task_store=resolved_long_task_store,
        tools=tools,
        operator=operator,
        approvals=approvals,
        progress_reader=progress_reader,
        memory_store=resolved_memory_store,
        memory_manager=memory_manager,
        mcp_client=mcp_client,
    )


def _resolve_planner(
    *,
    planner: AgentPlanner | None,
    model: ModelClient | None,
    tools: ToolRegistry,
) -> AgentPlanner:
    if planner is not None:
        return planner
    if model is not None:
        return JsonPlanPlanner(
            model,
            tool_schemas=tools.to_planning_schema(include_confirm=True),
        )
    raise ValueError("planner or model is required")


def _validate_planner_inputs(
    *,
    planner: AgentPlanner | None,
    model: ModelClient | None,
) -> None:
    if planner is not None and model is not None:
        raise ValueError("pass planner or model, not both")
    if planner is None and model is None:
        raise ValueError("planner or model is required")


def _build_store(config: ApplicationBootstrapConfig) -> TaskStore:
    if config.sqlite_path is not None:
        return SqliteTaskStore(config.sqlite_path)
    return InMemoryTaskStore()


def _build_long_task_store(config: ApplicationBootstrapConfig) -> LongTaskStore:
    if config.sqlite_path is not None:
        return SqliteLongTaskStore(config.sqlite_path)
    return InMemoryLongTaskStore()


def _build_memory_store(config: ApplicationBootstrapConfig) -> MemoryStore:
    if config.sqlite_path is not None:
        return SqliteMemoryStore(config.sqlite_path)
    return InMemoryMemoryStore()


def _build_conversation_store(
    config: ApplicationBootstrapConfig,
) -> ConversationStore:
    if config.sqlite_path is not None:
        return SqliteConversationStore(
            config.sqlite_path,
            raw_retention_rounds=config.conversation_raw_retention_rounds,
        )
    return InMemoryConversationStore(
        raw_retention_rounds=config.conversation_raw_retention_rounds,
    )


def _close_resource(resource: object) -> None:
    close = getattr(resource, "close", None)
    if callable(close):
        close()
