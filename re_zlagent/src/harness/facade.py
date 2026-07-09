"""Read-only harness facade for application and gateway layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from harness.skills import FileSystemSkillLoader, SkillManifest
from harness.storage import TaskStore
from harness.tools import Tool, ToolRegistry


@dataclass(frozen=True, slots=True)
class ToolInventoryItem:
    """Serializable description of a registered tool boundary."""

    name: str
    description: str
    permission: str
    is_read_only: bool
    is_destructive: bool
    is_concurrency_safe: bool
    side_effects: tuple[str, ...] = field(default_factory=tuple)
    interrupt_behavior: str = "block"

    def __post_init__(self) -> None:
        object.__setattr__(self, "side_effects", tuple(self.side_effects))

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "permission": self.permission,
            "is_read_only": self.is_read_only,
            "is_destructive": self.is_destructive,
            "is_concurrency_safe": self.is_concurrency_safe,
            "side_effects": list(self.side_effects),
            "interrupt_behavior": self.interrupt_behavior,
        }


@dataclass(frozen=True, slots=True)
class SkillInventoryItem:
    """Serializable description of a loaded skill."""

    id: str
    name: str
    description: str
    version: str
    format: str
    tags: tuple[str, ...] = field(default_factory=tuple)
    triggers: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "tags", tuple(self.tags))
        object.__setattr__(self, "triggers", tuple(self.triggers))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "format": self.format,
            "tags": list(self.tags),
            "triggers": list(self.triggers),
        }


@dataclass(frozen=True, slots=True)
class HarnessInventory:
    """Current read-only capability inventory."""

    tools: tuple[ToolInventoryItem, ...] = field(default_factory=tuple)
    skills: tuple[SkillInventoryItem, ...] = field(default_factory=tuple)
    errors: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "tools", tuple(self.tools))
        object.__setattr__(self, "skills", tuple(self.skills))
        object.__setattr__(self, "errors", tuple(self.errors))

    @property
    def counts(self) -> dict[str, int]:
        return {
            "tools": len(self.tools),
            "skills": len(self.skills),
            "errors": len(self.errors),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "tools": [item.to_dict() for item in self.tools],
            "skills": [item.to_dict() for item in self.skills],
            "errors": list(self.errors),
            "counts": self.counts,
        }


@dataclass(frozen=True, slots=True)
class HarnessRuntimeSnapshot:
    """Read-only component availability snapshot."""

    tool_registry: bool = False
    skill_loader: bool = False
    task_store: bool = False
    progress_reader: bool = False
    evals: bool = False
    observability: bool = False
    inventory_counts: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "inventory_counts", dict(self.inventory_counts))

    def to_dict(self) -> dict[str, Any]:
        return {
            "components": {
                "tool_registry": self.tool_registry,
                "skill_loader": self.skill_loader,
                "task_store": self.task_store,
                "progress_reader": self.progress_reader,
                "evals": self.evals,
                "observability": self.observability,
            },
            "inventory_counts": dict(self.inventory_counts),
        }


@dataclass(slots=True)
class HarnessFacade:
    """Application-facing read-only harness facade.

    The facade owns no business behavior. It derives snapshots from existing
    subsystems so app/gateway code does not import concrete registries directly.
    """

    tool_registry: ToolRegistry | None = None
    skill_loader: FileSystemSkillLoader | None = None
    task_store: TaskStore | None = None
    progress_reader: Any | None = None
    eval_runner: Any | None = None
    trace_recorder: Any | None = None

    def inventory(self) -> HarnessInventory:
        errors: list[str] = []
        tools = self._tools(errors)
        skills = self._skills(errors)
        return HarnessInventory(
            tools=tuple(sorted(tools, key=lambda item: item.name)),
            skills=tuple(sorted(skills, key=lambda item: item.id)),
            errors=tuple(errors),
        )

    def runtime(self) -> HarnessRuntimeSnapshot:
        inventory = self.inventory()
        return HarnessRuntimeSnapshot(
            tool_registry=self.tool_registry is not None,
            skill_loader=self.skill_loader is not None,
            task_store=self.task_store is not None,
            progress_reader=self.progress_reader is not None,
            evals=self.eval_runner is not None,
            observability=self.trace_recorder is not None,
            inventory_counts=inventory.counts,
        )

    def _tools(self, errors: list[str]) -> list[ToolInventoryItem]:
        if self.tool_registry is None:
            return []
        items: list[ToolInventoryItem] = []
        try:
            names = self.tool_registry.names()
        except Exception as exc:  # noqa: BLE001 - facade snapshots must not crash app boot
            errors.append(f"tool_registry: {type(exc).__name__}: {exc}")
            return []
        for name in names:
            tool = self.tool_registry.get(name)
            if tool is None:
                continue
            items.append(_tool_item(tool))
        return items

    def _skills(self, errors: list[str]) -> list[SkillInventoryItem]:
        if self.skill_loader is None:
            return []
        try:
            manifests = self.skill_loader.list()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"skill_loader: {type(exc).__name__}: {exc}")
            return []
        return [_skill_item(manifest) for manifest in manifests]


def build_harness_facade(
    *,
    tool_registry: ToolRegistry | None = None,
    skill_loader: FileSystemSkillLoader | None = None,
    task_store: TaskStore | None = None,
    progress_reader: Any | None = None,
    eval_runner: Any | None = None,
    trace_recorder: Any | None = None,
) -> HarnessFacade:
    """Construct a read-only harness facade."""

    return HarnessFacade(
        tool_registry=tool_registry,
        skill_loader=skill_loader,
        task_store=task_store,
        progress_reader=progress_reader,
        eval_runner=eval_runner,
        trace_recorder=trace_recorder,
    )


def _tool_item(tool: Tool) -> ToolInventoryItem:
    return ToolInventoryItem(
        name=tool.name,
        description=tool.description,
        permission=tool.permission.value,
        is_read_only=tool.is_read_only,
        is_destructive=tool.is_destructive,
        is_concurrency_safe=tool.is_concurrency_safe,
        side_effects=tool.side_effects,
        interrupt_behavior=tool.interrupt_behavior,
    )


def _skill_item(manifest: SkillManifest) -> SkillInventoryItem:
    return SkillInventoryItem(
        id=manifest.id,
        name=manifest.name,
        description=manifest.description,
        version=manifest.version,
        format=manifest.format.value,
        tags=manifest.tags,
        triggers=manifest.triggers,
    )
