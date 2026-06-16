"""write_file: write a UTF-8 text file under workspace_dir (confirm tool).

Permission tier is :data:`ToolPermission.CONFIRM`, so :class:`AgentLoop`
suspends the LLM turn and asks the user to approve before this code runs.
The execute() method itself is unconditional once invoked — confirmation
gating lives at the agent layer, not here.

Security model mirrors :mod:`backend.tools.builtins.read_file`:

* Paths are **relative** to the configured workspace root.
* Absolute paths and any path containing ``..`` are refused.
* After resolution the target must still live inside the workspace
  (defends against symlink escapes).
* Refuses to overwrite a directory.
* Hard cap: 256 KiB UTF-8 bytes per write — large enough for skill
  prompts and small notes, small enough that an accidental dump is
  visible in the confirmation dialog.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..base import Tool, ToolPermission, ToolResult

MAX_BYTES = 256 * 1024


class WriteFileTool(Tool):
    name = "write_file"
    description = (
        "Write a UTF-8 text file under the agent's workspace directory."
        " Paths must be relative to the workspace root, e.g."
        " 'skills/example-ping/instructions.md'. Up to 256 KiB per write."
        " This tool is permission=confirm: every invocation triggers an"
        " IM yes/no question to the user before it runs. Refuses absolute"
        " paths, paths that escape the workspace, and overwriting"
        " directories."
    )
    permission = ToolPermission.CONFIRM
    is_read_only = False
    is_concurrency_safe = False
    is_destructive = True
    max_result_chars = 2_000
    search_hint = "write create update file workspace text"
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Path relative to the workspace root. Use forward slashes."
                ),
            },
            "content": {
                "type": "string",
                "description": "UTF-8 text content to write. Must fit in 256 KiB.",
            },
            "create_parents": {
                "type": "boolean",
                "description": "Create parent directories as needed (default true).",
            },
        },
        "required": ["path", "content"],
    }

    def __init__(self, workspace_dir: Path) -> None:
        self._root = Path(workspace_dir).resolve()

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        rel = str(arguments.get("path") or "").strip()
        if not rel:
            return ToolResult(ok=False, content="", error="path is required")

        content = arguments.get("content")
        if not isinstance(content, str):
            return ToolResult(ok=False, content="", error="content must be a string")
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_BYTES:
            return ToolResult(
                ok=False, content="",
                error=f"content is {len(encoded)} bytes, exceeds 256 KiB cap",
            )

        rel = rel.replace("\\", "/")
        candidate = Path(rel)
        if candidate.is_absolute() or any(part == ".." for part in candidate.parts):
            return ToolResult(
                ok=False, content="", error=f"path must stay inside workspace: {rel}",
            )
        target = (self._root / candidate).resolve()
        try:
            target.relative_to(self._root)
        except ValueError:
            return ToolResult(
                ok=False, content="", error=f"path escapes workspace: {rel}",
            )

        if target.exists() and target.is_dir():
            return ToolResult(
                ok=False, content="", error=f"refusing to overwrite directory: {rel}",
            )

        create_parents = bool(arguments.get("create_parents", True))
        try:
            if create_parents:
                target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(encoded)
        except OSError as exc:
            return ToolResult(ok=False, content="", error=f"write failed: {exc}")

        return ToolResult(
            ok=True,
            content=f"Wrote {len(encoded)} bytes to {rel}",
        )
