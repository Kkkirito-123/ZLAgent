"""Workspace path boundary policy.

This module performs no file mutation. It only answers whether a user-provided
path can resolve inside the configured workspace root.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PureWindowsPath


class PathViolationType(str, Enum):
    """Stable path policy error codes."""

    INVALID_ROOT = "invalid_root"
    EMPTY_PATH = "empty_path"
    ABSOLUTE_PATH = "absolute_path"
    PARENT_REFERENCE = "parent_reference"
    ESCAPES_WORKSPACE = "escapes_workspace"


class WorkspacePathError(ValueError):
    """Raised when a path violates the workspace boundary."""

    def __init__(self, code: PathViolationType, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ResolvedWorkspacePath:
    """A path that has been normalized and proven to stay within the workspace."""

    root: Path
    relative_path: str
    absolute_path: Path


class WorkspacePathPolicy:
    """Resolve relative paths under a single workspace root."""

    def __init__(self, workspace_dir: Path | str) -> None:
        root = Path(workspace_dir).expanduser().resolve()
        if not root.exists() or not root.is_dir():
            raise WorkspacePathError(
                PathViolationType.INVALID_ROOT,
                f"workspace root must be an existing directory: {root}",
            )
        self.root = root

    def normalize_relative_path(self, value: object) -> str:
        raw = str(value or "").strip()
        if not raw:
            raise WorkspacePathError(
                PathViolationType.EMPTY_PATH,
                "path is required",
            )

        normalized = raw.replace("\\", "/")
        candidate = Path(normalized)
        if candidate.is_absolute() or PureWindowsPath(normalized).drive:
            raise WorkspacePathError(
                PathViolationType.ABSOLUTE_PATH,
                f"path must be relative to workspace: {normalized}",
            )
        if any(part == ".." for part in candidate.parts):
            raise WorkspacePathError(
                PathViolationType.PARENT_REFERENCE,
                f"path must not contain '..': {normalized}",
            )
        return candidate.as_posix()

    def resolve(self, value: object) -> ResolvedWorkspacePath:
        relative_path = self.normalize_relative_path(value)
        absolute_path = (self.root / Path(relative_path)).resolve(strict=False)
        try:
            absolute_path.relative_to(self.root)
        except ValueError as exc:
            raise WorkspacePathError(
                PathViolationType.ESCAPES_WORKSPACE,
                f"path escapes workspace: {relative_path}",
            ) from exc

        return ResolvedWorkspacePath(
            root=self.root,
            relative_path=relative_path,
            absolute_path=absolute_path,
        )
