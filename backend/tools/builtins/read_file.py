"""read_file: safely read a UTF-8 text file from within the agent workspace.

Security model (minimal but strict):

* Paths are **relative** to the configured workspace root. Absolute paths,
  paths containing ``..``, and paths that resolve outside the root after
  symlink expansion are all refused.
* Binary files are refused by a cheap NUL-byte heuristic in the first 1 KiB;
  this keeps the LLM from ingesting binary blobs that would burn tokens and
  produce garbage.
* Size is capped at 256 KiB; larger files are truncated with a visible
  marker so the LLM knows it is not seeing the whole thing.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING

from loguru import logger

from ..base import Tool, ToolPermission, ToolResult

if TYPE_CHECKING:
    from ...skills.usage import UsageStore

MAX_BYTES = 256 * 1024
PROBE_BYTES = 1024


class ReadFileTool(Tool):
    name = "read_file"
    description = (
        "Read a UTF-8 text file from within the agent's workspace directory."
        " Paths must be relative to the workspace root, e.g."
        " 'skills/example-ping/instructions.md'. Returns up to 256 KiB of"
        " text; refuses absolute paths, paths that escape the workspace,"
        " symlink escapes, and binary files."
    )
    permission = ToolPermission.SAFE
    is_read_only = True
    is_concurrency_safe = True
    is_destructive = False
    max_result_chars = 8_000
    search_hint = "workspace file read inspect skills markdown text"
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Path relative to the workspace root. Use forward slashes."
                ),
            }
        },
        "required": ["path"],
    }

    def __init__(
        self,
        workspace_dir: Path,
        *,
        usage_store: Optional[UsageStore] = None,
    ) -> None:
        self._root = Path(workspace_dir).resolve()
        self._usage = usage_store

    @staticmethod
    def _skill_name_for(rel: str) -> Optional[str]:
        """Map ``skills/<name>/SKILL.md`` → ``<name>``, otherwise None."""
        norm = rel.replace("\\", "/").lstrip("./")
        parts = norm.split("/")
        if len(parts) != 3 or parts[0] != "skills" or parts[2] != "SKILL.md":
            return None
        name = parts[1].strip()
        return name or None

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        rel = str(arguments.get("path") or "").strip()
        if not rel:
            return ToolResult(ok=False, content="", error="path is required")

        rel = rel.replace("\\", "/")
        candidate_path = Path(rel)
        if candidate_path.is_absolute() or any(part == ".." for part in candidate_path.parts):
            return ToolResult(
                ok=False, content="", error=f"path must stay inside workspace: {rel}"
            )

        resolved = (self._root / candidate_path).resolve()
        try:
            resolved.relative_to(self._root)
        except ValueError:
            return ToolResult(
                ok=False, content="", error=f"path escapes workspace: {rel}"
            )

        if not resolved.exists():
            return ToolResult(ok=False, content="", error=f"file not found: {rel}")
        if not resolved.is_file():
            return ToolResult(ok=False, content="", error=f"not a regular file: {rel}")

        try:
            data = resolved.read_bytes()
        except OSError as exc:
            return ToolResult(ok=False, content="", error=f"read failed: {exc}")

        if b"\x00" in data[:PROBE_BYTES]:
            return ToolResult(
                ok=False, content="", error="file appears to be binary (NUL byte in header)"
            )

        truncated = False
        if len(data) > MAX_BYTES:
            data = data[:MAX_BYTES]
            truncated = True

        text = data.decode("utf-8", errors="replace")
        suffix = "\n\n[...truncated]" if truncated else ""
        header = f"Path: {rel}\nBytes: {len(data)}{' (truncated)' if truncated else ''}\n\n"

        # v0.11 telemetry: agent (or user via slash command) just read a
        # skill's SKILL.md — bump its ``use_count``. This is the strongest
        # signal that a skill is "alive" because it implies the agent
        # decided the body was worth fully fetching.
        if self._usage is not None:
            skill_name = self._skill_name_for(rel)
            if skill_name:
                try:
                    self._usage.record_use(skill_name)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("usage record_use failed for {}: {}", skill_name, exc)

        return ToolResult(ok=True, content=header + text + suffix)
