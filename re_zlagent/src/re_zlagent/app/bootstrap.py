"""Application bootstrap helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from re_zlagent.harness import HarnessFacade, build_harness_facade
from re_zlagent.harness.agent import AgentOrchestrator, AgentPlanner, JsonPlanPlanner
from re_zlagent.harness.model import ModelClient
from re_zlagent.harness.progress import TaskProgressReader
from re_zlagent.harness.runtime import HarnessRuntime
from re_zlagent.harness.storage import InMemoryTaskStore, SqliteTaskStore, TaskStore
from re_zlagent.harness.tools import ToolRegistry
from re_zlagent.harness.tools.builtins import create_file_tools

from .application import AgentApplication


@dataclass(frozen=True, slots=True)
class ApplicationBootstrapConfig:
    """Configuration for assembling an AgentApplication container."""

    workspace_dir: Path | None = None
    sqlite_path: Path | None = None
    register_file_tools: bool = True

    def __post_init__(self) -> None:
        if self.workspace_dir is not None:
            object.__setattr__(self, "workspace_dir", Path(self.workspace_dir))
        if self.sqlite_path is not None:
            object.__setattr__(self, "sqlite_path", Path(self.sqlite_path))


@dataclass(frozen=True, slots=True)
class ApplicationContainer:
    """Concrete assembled application components."""

    app: AgentApplication
    facade: HarnessFacade
    orchestrator: AgentOrchestrator
    runtime: HarnessRuntime
    store: TaskStore
    tools: ToolRegistry
    planner: AgentPlanner

    def close(self) -> None:
        """Close owned resources when adapters expose a close method."""

        close = getattr(self.store, "close", None)
        if callable(close):
            close()


def build_application_container(
    *,
    planner: AgentPlanner | None = None,
    model: ModelClient | None = None,
    store: TaskStore | None = None,
    tool_registry: ToolRegistry | None = None,
    config: ApplicationBootstrapConfig | None = None,
) -> ApplicationContainer:
    """Build the minimal re_zlagent application graph."""

    cfg = config or ApplicationBootstrapConfig()
    resolved_planner = _resolve_planner(planner=planner, model=model)
    resolved_store = store or _build_store(cfg)
    tools = tool_registry or ToolRegistry()
    if cfg.register_file_tools and cfg.workspace_dir is not None:
        for tool in create_file_tools(cfg.workspace_dir):
            tools.register(tool)

    runtime = HarnessRuntime(store=resolved_store, tools=tools)
    orchestrator = AgentOrchestrator(planner=resolved_planner, runtime=runtime)
    app = AgentApplication(orchestrator=orchestrator)
    progress_reader = TaskProgressReader(resolved_store)
    facade = build_harness_facade(
        tool_registry=tools,
        task_store=resolved_store,
        progress_reader=progress_reader,
    )
    return ApplicationContainer(
        app=app,
        facade=facade,
        orchestrator=orchestrator,
        runtime=runtime,
        store=resolved_store,
        tools=tools,
        planner=resolved_planner,
    )


def _resolve_planner(
    *,
    planner: AgentPlanner | None,
    model: ModelClient | None,
) -> AgentPlanner:
    if planner is not None and model is not None:
        raise ValueError("pass planner or model, not both")
    if planner is not None:
        return planner
    if model is not None:
        return JsonPlanPlanner(model)
    raise ValueError("planner or model is required")


def _build_store(config: ApplicationBootstrapConfig) -> TaskStore:
    if config.sqlite_path is not None:
        return SqliteTaskStore(config.sqlite_path)
    return InMemoryTaskStore()
