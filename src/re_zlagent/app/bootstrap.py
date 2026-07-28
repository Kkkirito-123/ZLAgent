"""Application bootstrap helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from re_zlagent.harness import HarnessFacade, build_harness_facade
from re_zlagent.harness.agent import AgentOrchestrator, AgentPlanner, JsonPlanPlanner
from re_zlagent.harness.model import ModelClient
from re_zlagent.harness.progress import TaskProgressReader
from re_zlagent.harness.runtime import HarnessRuntime
from re_zlagent.harness.skills import FileSystemSkillLoader, LocalSkillInstaller
from re_zlagent.harness.storage import (
    InMemoryLongTaskStore,
    InMemoryTaskStore,
    LongTaskStore,
    SqliteLongTaskStore,
    SqliteTaskStore,
    TaskStore,
)
from re_zlagent.harness.tools import ToolRegistry
from re_zlagent.harness.tools.builtins import InstallSkillTool, create_file_tools

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

    def __post_init__(self) -> None:
        if self.workspace_dir is not None:
            object.__setattr__(self, "workspace_dir", Path(self.workspace_dir))
        if self.sqlite_path is not None:
            object.__setattr__(self, "sqlite_path", Path(self.sqlite_path))
        if self.skill_import_dir is not None:
            object.__setattr__(self, "skill_import_dir", Path(self.skill_import_dir))
        if self.skills_dir is not None:
            object.__setattr__(self, "skills_dir", Path(self.skills_dir))
        if (self.skill_import_dir is None) != (self.skills_dir is None):
            raise ValueError(
                "skill_import_dir and skills_dir must be configured together"
            )


@dataclass(frozen=True, slots=True)
class ApplicationContainer:
    """Concrete assembled application components."""

    app: AgentApplication
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

    def close(self) -> None:
        """Close owned resources when adapters expose a close method."""

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

    def close(self) -> None:
        """Close owned resources when adapters expose a close method."""

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
    store: TaskStore | None = None,
    long_task_store: LongTaskStore | None = None,
    tool_registry: ToolRegistry | None = None,
    config: ApplicationBootstrapConfig | None = None,
) -> ApplicationContainer:
    """Build the minimal re_zlagent application graph."""

    cfg = config or ApplicationBootstrapConfig()
    _validate_planner_inputs(planner=planner, model=model)
    services = build_application_runtime(
        store=store,
        long_task_store=long_task_store,
        tool_registry=tool_registry,
        config=cfg,
    )
    resolved_planner = _resolve_planner(
        planner=planner,
        model=model,
        tools=services.tools,
    )
    orchestrator = AgentOrchestrator(
        planner=resolved_planner,
        runtime=services.runtime,
    )
    app = AgentApplication(orchestrator=orchestrator)
    return ApplicationContainer(
        app=app,
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
    )


def build_application_runtime(
    *,
    store: TaskStore | None = None,
    long_task_store: LongTaskStore | None = None,
    tool_registry: ToolRegistry | None = None,
    config: ApplicationBootstrapConfig | None = None,
) -> ApplicationRuntimeContainer:
    """Build runtime/operator services without requiring model configuration."""

    cfg = config or ApplicationBootstrapConfig()
    resolved_store = store or _build_store(cfg)
    resolved_long_task_store = long_task_store or _build_long_task_store(cfg)
    tools = tool_registry or ToolRegistry()
    if cfg.register_file_tools and cfg.workspace_dir is not None:
        for tool in create_file_tools(cfg.workspace_dir):
            tools.register(tool)
    skill_loader: FileSystemSkillLoader | None = None
    if cfg.skill_import_dir is not None and cfg.skills_dir is not None:
        skill_loader = FileSystemSkillLoader(cfg.skills_dir)
        skill_loader.load()
        installer = LocalSkillInstaller(
            cfg.skill_import_dir,
            cfg.skills_dir,
            loader=skill_loader,
        )
        tools.register(InstallSkillTool(installer))

    runtime = HarnessRuntime(
        store=resolved_store,
        tools=tools,
        long_task_store=resolved_long_task_store,
    )
    operator = OperatorService(resolved_store)
    approvals = ApprovalService(runtime)
    progress_reader = TaskProgressReader(resolved_store)
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
