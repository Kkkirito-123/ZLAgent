"""Read-only task-run branch tree projected from existing fork lineage."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from re_zlagent.harness.storage import TaskStore
from re_zlagent.harness.tasking import TaskRun


class BranchLineageError(ValueError):
    """Raised when persisted fork lineage is missing or cyclic."""


@dataclass(frozen=True, slots=True)
class RunBranchNode:
    """One run in a projected branch tree."""

    run_id: str
    status: str
    parent_run_id: str | None = None
    forked_from_checkpoint_id: str | None = None
    selected: bool = False
    children: tuple["RunBranchNode", ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.run_id.strip():
            raise ValueError("branch node run_id must be non-empty")
        object.__setattr__(self, "children", tuple(self.children))

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "parent_run_id": self.parent_run_id,
            "forked_from_checkpoint_id": self.forked_from_checkpoint_id,
            "selected": self.selected,
            "children": [child.to_dict() for child in self.children],
        }


@dataclass(frozen=True, slots=True)
class RunBranchTree:
    """One immutable read model rooted at the original run."""

    selected_run_id: str
    root: RunBranchNode
    node_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "selected_run_id": self.selected_run_id,
            "root_run_id": self.root.run_id,
            "node_count": self.node_count,
            "root": self.root.to_dict(),
        }


class RunBranchTreeBuilder:
    """Build a small branch tree without copying events or task state."""

    def __init__(self, store: TaskStore, *, max_nodes: int = 500) -> None:
        if max_nodes < 1:
            raise ValueError("branch max_nodes must be >= 1")
        self._store = store
        self._max_nodes = max_nodes

    def build(self, run_id: str) -> RunBranchTree:
        selected = self._store.get_run(run_id)
        if selected is None:
            raise BranchLineageError(f"unknown run id: {run_id}")
        runs = {
            run.id: run
            for run in self._store.list_runs(limit=self._max_nodes)
        }
        runs.setdefault(selected.id, selected)
        root_id = self._root_id(selected, runs)
        children: dict[str, list[TaskRun]] = {}
        for run in runs.values():
            parent_id = _parent_id(run)
            if parent_id is not None and parent_id in runs:
                children.setdefault(parent_id, []).append(run)
        count = 0

        def project(current_id: str, path: tuple[str, ...]) -> RunBranchNode:
            nonlocal count
            if current_id in path:
                raise BranchLineageError(
                    "branch lineage contains a cycle: "
                    + " -> ".join((*path, current_id))
                )
            current = runs[current_id]
            count += 1
            if count > self._max_nodes:
                raise BranchLineageError(
                    "branch tree exceeds configured node limit"
                )
            child_nodes = tuple(
                project(child.id, (*path, current_id))
                for child in sorted(
                    children.get(current_id, ()),
                    key=lambda item: item.id,
                )
            )
            return RunBranchNode(
                run_id=current.id,
                status=current.status.value,
                parent_run_id=_parent_id(current),
                forked_from_checkpoint_id=_checkpoint_id(current),
                selected=current.id == run_id,
                children=child_nodes,
            )

        root = project(root_id, ())
        return RunBranchTree(
            selected_run_id=run_id,
            root=root,
            node_count=count,
        )

    def _root_id(
        self,
        selected: TaskRun,
        runs: dict[str, TaskRun],
    ) -> str:
        current = selected
        seen: list[str] = []
        while True:
            if current.id in seen:
                raise BranchLineageError(
                    "branch lineage contains a cycle: "
                    + " -> ".join((*seen, current.id))
                )
            seen.append(current.id)
            parent_id = _parent_id(current)
            if parent_id is None:
                return current.id
            parent = runs.get(parent_id)
            if parent is None:
                raise BranchLineageError(
                    f"branch lineage references missing parent: {parent_id}"
                )
            current = parent


def _parent_id(run: TaskRun) -> str | None:
    value = run.metadata.get("forked_from_run_id")
    return value if isinstance(value, str) and value.strip() else None


def _checkpoint_id(run: TaskRun) -> str | None:
    value = run.metadata.get("forked_from_checkpoint_id")
    return value if isinstance(value, str) and value.strip() else None
