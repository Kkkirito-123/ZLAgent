"""Workspace file tools with structured safety metadata."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Any

from ...sandbox import PathViolationType, WorkspacePathError, WorkspacePathPolicy
from ..base import Tool, ToolExecutionContext, ToolPermission, ToolResult
from ..metadata import Evidence, RecommendedNextAction, SideEffect, ToolErrorType
from ..read_before_write import ReadBeforeWritePolicy

MAX_BYTES = 256 * 1024
PROBE_BYTES = 1024


def _sha256_hex(data: bytes) -> str:
    return sha256(data).hexdigest()


def _path_error_result(
    *,
    tool_name: str,
    error: WorkspacePathError,
    writing: bool,
) -> ToolResult:
    unsafe_codes = {
        PathViolationType.ABSOLUTE_PATH,
        PathViolationType.PARENT_REFERENCE,
        PathViolationType.ESCAPES_WORKSPACE,
    }
    return ToolResult.failure(
        error.message,
        error_type=(
            ToolErrorType.UNSAFE_WRITE
            if writing and error.code in unsafe_codes
            else ToolErrorType.INVALID_INPUT
        ),
        recoverable_by_model=error.code is PathViolationType.EMPTY_PATH,
        recommended_next_action=(
            RecommendedNextAction.STOP
            if error.code in unsafe_codes
            else RecommendedNextAction.RETRY
        ),
        raw={"path_error": error.code.value},
        source=tool_name,
    )


class ReadFileTool(Tool):
    """Read a UTF-8 text file from the configured workspace."""

    name = "read_file"
    description = (
        "Read a UTF-8 text file from within the workspace. Paths must be "
        "relative to the workspace root. Refuses absolute paths, parent "
        "references, symlink escapes, directories, and binary files."
    )
    permission = ToolPermission.SAFE
    is_read_only = True
    is_concurrency_safe = True
    is_destructive = False
    max_result_chars = 8_000
    input_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path relative to the workspace root.",
            }
        },
        "required": ["path"],
    }

    def __init__(
        self,
        workspace_dir: Path | str,
        *,
        read_before_write_policy: ReadBeforeWritePolicy | None = None,
        path_policy: WorkspacePathPolicy | None = None,
    ) -> None:
        self._path_policy = path_policy or WorkspacePathPolicy(workspace_dir)
        self._read_before_write = read_before_write_policy or ReadBeforeWritePolicy()

    @property
    def read_before_write_policy(self) -> ReadBeforeWritePolicy:
        return self._read_before_write

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        try:
            resolved = self._path_policy.resolve(arguments.get("path"))
        except WorkspacePathError as exc:
            return _path_error_result(tool_name=self.name, error=exc, writing=False)

        rel = resolved.relative_path
        target = resolved.absolute_path
        if not target.exists():
            return ToolResult.failure(
                f"file not found: {rel}",
                error_type=ToolErrorType.NOT_FOUND,
                recoverable_by_model=True,
                recommended_next_action=RecommendedNextAction.RETRY,
                source=self.name,
            )
        if not target.is_file():
            return ToolResult.failure(
                f"not a regular file: {rel}",
                error_type=ToolErrorType.INVALID_INPUT,
                recoverable_by_model=True,
                recommended_next_action=RecommendedNextAction.RETRY,
                source=self.name,
            )

        try:
            full_data = target.read_bytes()
        except OSError as exc:
            return ToolResult.failure(
                f"read failed: {exc}",
                error_type=ToolErrorType.EXTERNAL_UNAVAILABLE,
                recoverable_by_model=False,
                recommended_next_action=RecommendedNextAction.MANUAL_REVIEW,
                source=self.name,
            )

        if b"\x00" in full_data[:PROBE_BYTES]:
            return ToolResult.failure(
                "file appears to be binary (NUL byte in header)",
                error_type=ToolErrorType.INVALID_INPUT,
                recoverable_by_model=False,
                recommended_next_action=RecommendedNextAction.STOP,
                source=self.name,
            )

        full_hash = _sha256_hex(full_data)
        truncated = len(full_data) > MAX_BYTES
        returned_data = full_data[:MAX_BYTES] if truncated else full_data
        text = returned_data.decode("utf-8", errors="replace")
        suffix = "\n\n[...truncated]" if truncated else ""
        header = (
            f"Path: {rel}\n"
            f"Bytes: {len(full_data)}"
            f"{' (truncated)' if truncated else ''}\n"
            f"SHA256: {full_hash}\n\n"
        )

        self._read_before_write.stamp_read(
            target=str(target),
            content_hash=full_hash,
            metadata={
                "relative_path": rel,
                "bytes": len(full_data),
                "tool": self.name,
            },
        )

        return ToolResult.success(
            header + text + suffix,
            raw={
                "path": rel,
                "bytes": len(full_data),
                "returned_bytes": len(returned_data),
                "sha256": full_hash,
                "truncated": truncated,
            },
            evidence=[
                Evidence(
                    type="file",
                    ref=rel,
                    summary="workspace file read",
                    metadata={
                        "absolute_path": str(target),
                        "bytes": len(full_data),
                        "returned_bytes": len(returned_data),
                        "sha256": full_hash,
                        "truncated": truncated,
                    },
                )
            ],
            source=self.name,
        )


class WriteFileTool(Tool):
    """Write a UTF-8 text file under the configured workspace."""

    name = "write_file"
    description = (
        "Write a UTF-8 text file under the workspace. Paths must be relative "
        "to the workspace root. Existing targets require a matching "
        "read-before-write mark."
    )
    permission = ToolPermission.CONFIRM
    is_read_only = False
    is_concurrency_safe = False
    is_destructive = True
    side_effects = ("filesystem",)
    outbox_required = True
    side_effect_retry_safe = True
    max_result_chars = 2_000
    input_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path relative to the workspace root.",
            },
            "content": {
                "type": "string",
                "description": "UTF-8 text content to write. Max 256 KiB.",
            },
            "create_parents": {
                "type": "boolean",
                "description": "Create parent directories as needed. Default true.",
            },
        },
        "required": ["path", "content"],
    }

    def __init__(
        self,
        workspace_dir: Path | str,
        *,
        read_before_write_policy: ReadBeforeWritePolicy | None = None,
        path_policy: WorkspacePathPolicy | None = None,
    ) -> None:
        self._path_policy = path_policy or WorkspacePathPolicy(workspace_dir)
        self._read_before_write = read_before_write_policy or ReadBeforeWritePolicy()

    @property
    def read_before_write_policy(self) -> ReadBeforeWritePolicy:
        return self._read_before_write

    def requires_read_before_write(self, arguments: dict[str, Any] | None) -> bool:
        return True

    def plan_side_effects(
        self,
        arguments: dict[str, Any],
    ) -> tuple[SideEffect, ...]:
        try:
            resolved = self._path_policy.resolve(arguments.get("path"))
        except WorkspacePathError as exc:
            raise ValueError(exc.message) from exc
        content = arguments.get("content")
        if not isinstance(content, str):
            raise ValueError("content must be a string")
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_BYTES:
            raise ValueError(
                f"content is {len(encoded)} bytes, exceeds 256 KiB cap"
            )
        target = resolved.absolute_path
        if target.exists() and target.is_dir():
            raise ValueError(
                f"refusing to overwrite directory: {resolved.relative_path}"
            )
        return (
            self._write_side_effect(
                relative_path=resolved.relative_path,
                absolute_path=target,
                encoded=encoded,
                creating_new_target=not target.exists(),
            ),
        )

    async def execute_with_context(
        self,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        """Treat an already-applied identical write as an idempotent replay."""

        try:
            resolved = self._path_policy.resolve(arguments.get("path"))
            content = arguments.get("content")
            if not isinstance(content, str):
                return await self.execute(arguments)
            encoded = content.encode("utf-8")
            target = resolved.absolute_path
            if target.is_file() and target.read_bytes() == encoded:
                return self._write_success_result(
                    relative_path=resolved.relative_path,
                    absolute_path=target,
                    encoded=encoded,
                    creating_new_target=False,
                    idempotent_replay=True,
                )
        except (OSError, WorkspacePathError):
            pass
        return await self.execute(arguments)

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        try:
            resolved = self._path_policy.resolve(arguments.get("path"))
        except WorkspacePathError as exc:
            return _path_error_result(tool_name=self.name, error=exc, writing=True)

        content = arguments.get("content")
        if not isinstance(content, str):
            return ToolResult.failure(
                "content must be a string",
                error_type=ToolErrorType.INVALID_INPUT,
                recoverable_by_model=True,
                recommended_next_action=RecommendedNextAction.RETRY,
                source=self.name,
            )

        encoded = content.encode("utf-8")
        if len(encoded) > MAX_BYTES:
            return ToolResult.failure(
                f"content is {len(encoded)} bytes, exceeds 256 KiB cap",
                error_type=ToolErrorType.INVALID_INPUT,
                recoverable_by_model=True,
                recommended_next_action=RecommendedNextAction.RETRY,
                source=self.name,
            )

        rel = resolved.relative_path
        target = resolved.absolute_path
        if target.exists() and target.is_dir():
            return ToolResult.failure(
                f"refusing to overwrite directory: {rel}",
                error_type=ToolErrorType.UNSAFE_WRITE,
                recoverable_by_model=False,
                recommended_next_action=RecommendedNextAction.STOP,
                source=self.name,
            )

        creating_new_target = not target.exists()
        current_hash: str | None = None
        if not creating_new_target:
            try:
                current_hash = _sha256_hex(target.read_bytes())
            except OSError as exc:
                return ToolResult.failure(
                    f"read before write failed: {exc}",
                    error_type=ToolErrorType.EXTERNAL_UNAVAILABLE,
                    recoverable_by_model=False,
                    recommended_next_action=RecommendedNextAction.MANUAL_REVIEW,
                    source=self.name,
                )

        precondition = self._read_before_write.check_write(
            target=str(target),
            current_hash=current_hash,
            creating_new_target=creating_new_target,
        )
        if not precondition.allowed:
            return ToolResult.failure(
                precondition.reason,
                error_type=ToolErrorType.UNSAFE_WRITE,
                recoverable_by_model=True,
                recommended_next_action=RecommendedNextAction.READ_BEFORE_WRITE,
                raw={
                    "path": rel,
                    "required_action": precondition.required_action,
                    "current_hash": current_hash,
                },
                evidence=[
                    Evidence(
                        type="write_precondition",
                        ref=rel,
                        summary=precondition.reason,
                        metadata={
                            "absolute_path": str(target),
                            "required_action": precondition.required_action,
                            "current_hash": current_hash,
                        },
                    )
                ],
                source=self.name,
            )

        create_parents = bool(arguments.get("create_parents", True))
        try:
            if create_parents:
                target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(encoded)
        except OSError as exc:
            return ToolResult.failure(
                f"write failed: {exc}",
                error_type=ToolErrorType.EXTERNAL_UNAVAILABLE,
                recoverable_by_model=False,
                recommended_next_action=RecommendedNextAction.MANUAL_REVIEW,
                source=self.name,
            )

        return self._write_success_result(
            relative_path=rel,
            absolute_path=target,
            encoded=encoded,
            creating_new_target=creating_new_target,
        )

    def _write_success_result(
        self,
        *,
        relative_path: str,
        absolute_path: Path,
        encoded: bytes,
        creating_new_target: bool,
        idempotent_replay: bool = False,
    ) -> ToolResult:
        new_hash = _sha256_hex(encoded)
        return ToolResult.success(
            f"Wrote {len(encoded)} bytes to {relative_path}",
            raw={
                "path": relative_path,
                "bytes": len(encoded),
                "sha256": new_hash,
                "created": creating_new_target,
                "idempotent_replay": idempotent_replay,
            },
            evidence=[
                Evidence(
                    type="file",
                    ref=relative_path,
                    summary="workspace file written",
                    metadata={
                        "absolute_path": str(absolute_path),
                        "bytes": len(encoded),
                        "sha256": new_hash,
                        "created": creating_new_target,
                        "idempotent_replay": idempotent_replay,
                    },
                )
            ],
            side_effects=[
                self._write_side_effect(
                    relative_path=relative_path,
                    absolute_path=absolute_path,
                    encoded=encoded,
                    creating_new_target=creating_new_target,
                )
            ],
            source=self.name,
        )

    @staticmethod
    def _write_side_effect(
        *,
        relative_path: str,
        absolute_path: Path,
        encoded: bytes,
        creating_new_target: bool,
    ) -> SideEffect:
        return SideEffect(
            type="filesystem",
            target=relative_path,
            risk="medium" if not creating_new_target else "low",
            metadata={
                "absolute_path": str(absolute_path),
                "bytes": len(encoded),
                "sha256": _sha256_hex(encoded),
            },
        )


def create_file_tools(
    workspace_dir: Path | str,
    *,
    read_before_write_policy: ReadBeforeWritePolicy | None = None,
) -> tuple[ReadFileTool, WriteFileTool]:
    """Create read/write tools with shared path and read-before-write policy."""

    path_policy = WorkspacePathPolicy(workspace_dir)
    policy = read_before_write_policy or ReadBeforeWritePolicy()
    return (
        ReadFileTool(
            workspace_dir,
            read_before_write_policy=policy,
            path_policy=path_policy,
        ),
        WriteFileTool(
            workspace_dir,
            read_before_write_policy=policy,
            path_policy=path_policy,
        ),
    )
