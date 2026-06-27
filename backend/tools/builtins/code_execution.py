"""code_execution: run a short Python or shell snippet in a sandboxed subprocess.

Reuses the v0.10 ``backend/cron/pre_script.py`` infrastructure:

* asyncio ``create_subprocess_exec`` with hard timeout
* stdout cap (256 KiB) + truncation flag
* terminate → kill on timeout
* never raises into the caller; failures become ``ToolResult(ok=False)``

What's new vs ``pre_script``
----------------------------

``pre_script`` runs a *file path* under ``workspace/`` that the user
already wrote and whose existence the agent knows about. ``code_execution``
runs an *inline snippet* the LLM emits: we materialise it into a temp
file under ``workspace/.agent-runs/<timestamp>.<ext>`` so the script can
import workspace-local modules + read/write the workspace, and so the
operator can audit the exact code that ran (the file is left on disk
until the next invocation cleans up older entries).

Permission tier: **CONFIRM**. Every snippet gets an IM yes/no — running
arbitrary code on the bot host is exactly the kind of thing that should
not be auto-approved.

Sandboxing notes
----------------

The threat model here is "the LLM hallucinated something destructive and
the user clicked yes by accident", not "a hostile attacker has
compromised the LLM". For the former, three layers are enough:

1. ``cwd = workspace_dir.resolve()`` — anything resolved as a relative
   path stays inside the workspace
2. Hard timeout (default 30s, max 300s) prevents runaway loops
3. Stdout cap prevents 1 GB-of-printf attacks blowing out the LLM's
   context window

We deliberately do NOT try to be cgroup / seccomp clever — we already
trust the script the cron pre_script field runs, and that field uses
the same threat model. If you ever expose this to a multi-tenant or
public-facing deployment, swap ``backend/cron/pre_script.py``'s spawn
with a real sandbox (firejail / nsjail / docker exec) and this tool
inherits the upgrade.
"""
from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from ...runtime import HostTaskRuntime, SandboxPolicy
from ..base import Tool, ToolPermission, ToolResult

# Hard cap; matches pre_script for consistency.
MAX_STDOUT_BYTES = 256 * 1024
MAX_TIMEOUT_SECONDS = 300
DEFAULT_TIMEOUT_SECONDS = 30
MAX_CODE_BYTES = 64 * 1024  # generous — equivalent to ~16k tokens

# Where to dump materialised snippets so they're auditable on disk and
# the LLM (or operator) can refer back via ``read_file``.
RUNS_DIRNAME = ".agent-runs"
# Keep at most this many recent runs on disk; older ones get pruned at
# the start of every execution. Conservative value chosen so we don't
# hand the LLM unbounded write access to the workspace.
KEEP_RECENT_RUNS = 20


SUPPORTED_LANGUAGES = ("python", "shell")


class CodeExecutionTool(Tool):
    name = "code_execution"
    description = (
        "Run a short Python or shell snippet in a sandboxed subprocess and"
        " return its stdout. Use this for **temporary computation /"
        " data wrangling / one-off scripts** the agent needs to satisfy"
        " a turn — calculations, csv parsing, JSON munging, string"
        " manipulation, quick file inspection.\n\n"
        "Permission tier is **confirm**: every invocation triggers an IM"
        " yes/no, since we are running arbitrary code. The operator sees"
        " the full snippet in the confirmation message before approving.\n\n"
        "Sandboxing:\n"
        "* ``cwd`` is the workspace root, so relative paths stay inside.\n"
        "* Hard timeout (default 30s, max 300s; pick conservatively).\n"
        "* stdout cap 256 KiB; longer output is truncated with a flag.\n"
        "* The materialised script file is left under"
        " ``workspace/.agent-runs/`` so you can audit / re-run it later.\n\n"
        "**When to use**:\n"
        "* '帮我算 N!', '处理这段 csv', 'pip 一下看 X 的版本'.\n"
        "* '展开这段 json' (only if `read_file` won't do).\n\n"
        "**When NOT to use**:\n"
        "* Long-running data pipelines — that belongs in a cron job.\n"
        "* Anything that mutates external services (instead use the"
        " specific tool, e.g. ``cron_manage`` for cron jobs).\n"
        "* If the question can be answered without running code, do that"
        " instead — running code costs the operator a confirm prompt."
    )
    permission = ToolPermission.CONFIRM
    is_read_only = False
    is_concurrency_safe = False
    is_destructive = True
    max_result_chars = 8_000
    search_hint = "run execute python shell code calculation script subprocess"
    should_defer = True
    parameters_schema = {
        "type": "object",
        "properties": {
            "language": {
                "type": "string",
                "enum": list(SUPPORTED_LANGUAGES),
                "description": (
                    "``python`` runs the snippet under the bot's own Python"
                    " interpreter (so any dependency installed in the image"
                    " is available). ``shell`` runs under /bin/sh on Linux."
                ),
            },
            "code": {
                "type": "string",
                "description": (
                    "The full snippet, max 64 KiB. ``print`` / ``echo`` is"
                    " how you surface output back to the agent."
                ),
            },
            "timeout_seconds": {
                "type": "integer",
                "minimum": 1, "maximum": MAX_TIMEOUT_SECONDS,
                "default": DEFAULT_TIMEOUT_SECONDS,
                "description": (
                    "Hard wall-clock timeout. Pick the smallest plausible"
                    " value — the agent loop is paused for this many"
                    " seconds while the subprocess runs."
                ),
            },
            "description": {
                "type": "string",
                "description": (
                    "Optional one-line summary the operator sees in the"
                    " confirmation prompt — e.g. 'sum first column of"
                    " sales.csv'. Helps the user decide quickly without"
                    " parsing the snippet."
                ),
            },
        },
        "required": ["language", "code"],
    }

    def __init__(self, workspace_dir: Path) -> None:
        self._workspace = Path(workspace_dir).resolve()
        # single shared runtime per tool instance. Construction
        # is cheap (just resolves workspace_dir once); reusing it across
        # turns keeps the call site tiny.
        self._runtime = HostTaskRuntime(self._workspace)

    # =================================================================
    # entry point
    # =================================================================

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        language = str(arguments.get("language") or "").strip().lower()
        if language not in SUPPORTED_LANGUAGES:
            return ToolResult(
                ok=False, content="",
                error=f"language must be one of {SUPPORTED_LANGUAGES!r}",
            )
        code = arguments.get("code")
        if not isinstance(code, str) or not code.strip():
            return ToolResult(ok=False, content="", error="code must be a non-empty string")
        if len(code.encode("utf-8")) > MAX_CODE_BYTES:
            return ToolResult(
                ok=False, content="",
                error=f"code exceeds {MAX_CODE_BYTES} bytes; split or summarise",
            )
        try:
            timeout = int(arguments.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS)
        except (TypeError, ValueError):
            return ToolResult(
                ok=False, content="", error="timeout_seconds must be an integer",
            )
        if not 1 <= timeout <= MAX_TIMEOUT_SECONDS:
            return ToolResult(
                ok=False, content="",
                error=f"timeout_seconds must be between 1 and {MAX_TIMEOUT_SECONDS}",
            )

        # Pre-flight: shell needs an actual sh binary on PATH or /bin/sh.
        # Nicer message than HostTaskRuntime's "interpreter not found"
        # for the Windows-host-without-shell case.
        if language == "shell" and shutil.which("sh") is None and not Path("/bin/sh").exists():
            return ToolResult(
                ok=False, content="",
                error="sh not available on this host; shell language unsupported here",
            )

        # 1) Materialise the snippet under workspace/.agent-runs so the
        # operator can audit it after the fact, and so any imports
        # resolve relative to the workspace root.
        try:
            runs_dir = self._ensure_runs_dir()
        except OSError as exc:
            return ToolResult(
                ok=False, content="",
                error=f"failed to prepare runs directory: {exc}",
            )
        ext = ".py" if language == "python" else ".sh"
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target = runs_dir / f"run-{ts}{ext}"
        try:
            target.write_text(code, encoding="utf-8")
        except OSError as exc:
            return ToolResult(
                ok=False, content="",
                error=f"failed to write snippet: {exc}",
            )

        # 2) Run via HostTaskRuntime — same workspace, same SandboxPolicy
        # ceilings as pre_script. The runtime owns subprocess +
        # timeout + truncation + cleanup.
        rel_path = str(target.relative_to(self._workspace))
        logger.info(
            "running code_execution {} (timeout={}s, file={})",
            language, timeout, target.name,
        )
        policy = SandboxPolicy(
            timeout_seconds=timeout,
            max_output_bytes=MAX_STDOUT_BYTES,
        )
        rr = await self._runtime.execute(
            kind="script",
            payload=rel_path,
            policy=policy,
            labels={"purpose": "code_execution", "language": language},
        )

        # 3) Compose the operator-facing header from RuntimeResult; same
        # shape the v0.13 path produced so log scrapers stay happy.
        header = (
            f"language={language} exit={rr.exit_code if rr.exit_code is not None else 'n/a'}"
            f" duration={rr.duration_ms}ms file={rel_path}"
            f"{' (stdout truncated)' if rr.truncated_stdout else ''}\n"
        )

        # Timeout: preserve v0.13 wording for log scrapers.
        if rr.error and "timed out" in rr.error:
            return ToolResult(
                ok=False, content="",
                error=f"code_execution timed out after {timeout}s ({rr.duration_ms}ms wall)",
            )
        if not rr.ok:
            stderr_preview = (rr.stderr or "")[:800]
            if rr.exit_code is not None:
                err = f"exit={rr.exit_code}; stderr (truncated): {stderr_preview}"
            else:
                err = rr.error or "code_execution failed"
            return ToolResult(
                ok=False,
                content=header + (rr.stdout or ""),
                error=err,
            )
        return ToolResult(ok=True, content=header + rr.stdout)

    # =================================================================
    # helpers
    # =================================================================

    def _ensure_runs_dir(self) -> Path:
        runs_dir = self._workspace / RUNS_DIRNAME
        runs_dir.mkdir(parents=True, exist_ok=True)
        # Prune older runs so the workspace doesn't accumulate forever.
        try:
            entries = sorted(
                (p for p in runs_dir.iterdir() if p.is_file() and p.name.startswith("run-")),
                key=lambda p: p.stat().st_mtime,
            )
            excess = len(entries) - KEEP_RECENT_RUNS
            for old in entries[: max(0, excess)]:
                try:
                    old.unlink()
                except OSError:
                    pass
        except OSError as exc:
            logger.debug("[code_execution] runs dir prune failed: {}", exc)
        return runs_dir
