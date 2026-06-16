"""Pre-script runner for cron jobs (v0.10).

Some cron-driven skills are *data-bound* — "summarise today's arxiv",
"alert when CPU > 80%", "tell me what changed in repo X" — and asking
the LLM to produce that data from thin air is a recipe for hallucination.
Hermes Agent solves this with a ``pre_script`` field on each cron job:
just before the LLM is invoked the script is exec'd, its stdout is
spliced into the user-side instruction as ground-truth context, and the
LLM only formats / summarises.

This module is the ZLAgent equivalent. Design constraints:

* **Scoped to the workspace**: the script path must resolve under
  ``settings.workspace_dir``. We refuse absolute paths and any traversal
  that escapes the workspace (``../`` etc.). This is the same threat
  model as ``read_file`` / ``write_file``.
* **Extension allowlist**: ``.py`` and ``.sh`` only. Stops the LLM (or a
  poorly-validated API client) from registering a job that runs an
  arbitrary binary.
* **Bounded timeout**: a misbehaving script that hangs forever can't take
  down the cron loop because we wrap the call in
  :func:`asyncio.wait_for` and fall back to ``[ZLAgent pre_script
  timeout]`` text on expiry.
* **Bounded stdout**: at most :data:`MAX_STDOUT_BYTES` are captured, so
  a runaway data dump can't blow out the LLM's context window.

The runner returns a small dataclass instead of a bare string so the
caller (``run_cron_job`` in ``app.py``) can include status + execution
time in its log line and surface failures to the user via the
``last_error`` column.
"""
from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from loguru import logger

from ..runtime import HostTaskRuntime, SandboxPolicy

# Absolute caps — keep these tight, they are deliberately conservative.
MAX_STDOUT_BYTES = 256 * 1024  # ~256 KiB; matches read_file's cap
MAX_TIMEOUT_SECONDS = 300       # 5 minutes is generous for pre_script
ALLOWED_EXTENSIONS = (".py", ".sh")


@dataclass(slots=True)
class PreScriptResult:
    """Outcome of one pre-script invocation."""

    ok: bool
    stdout: str
    error: Optional[str]
    duration_ms: int
    truncated: bool = False


def _resolve_safe_path(workspace_dir: Path, raw: str) -> Path:
    """Return the absolute, validated path for a script under the workspace.

    Raises :class:`ValueError` with a human-readable reason on any
    rejection (absolute path, traversal escape, missing file, wrong
    extension, not a regular file).
    """
    if not raw or not raw.strip():
        raise ValueError("pre_script_path is empty")
    candidate_str = raw.strip()
    candidate = Path(candidate_str)
    if candidate.is_absolute():
        raise ValueError("pre_script_path must be a workspace-relative path")
    workspace_resolved = workspace_dir.resolve()
    target = (workspace_resolved / candidate).resolve()
    try:
        target.relative_to(workspace_resolved)
    except ValueError as exc:
        raise ValueError(
            f"pre_script_path {candidate_str!r} escapes workspace_dir"
        ) from exc
    if not target.exists():
        raise ValueError(f"pre_script_path {candidate_str!r} does not exist")
    if not target.is_file():
        raise ValueError(f"pre_script_path {candidate_str!r} is not a regular file")
    if target.suffix.lower() not in ALLOWED_EXTENSIONS:
        raise ValueError(
            f"pre_script_path {candidate_str!r} suffix not in"
            f" {ALLOWED_EXTENSIONS}; reject for safety"
        )
    return target


def _interpreter_for(target: Path) -> list[str]:
    """Pick the interpreter argv for ``target`` based on its suffix.

    We never let the OS resolve a script through ``execvp`` directly —
    that would invite issues with executable bits, shebang parsing
    differences, and on Windows the "double-click default app" trap.
    Always wrap in a known-good interpreter.
    """
    suffix = target.suffix.lower()
    if suffix == ".py":
        # Use the same Python the bot runs with so any pip-installed deps
        # are visible to the script. ``shutil.which("python")`` would
        # also work but ``sys.executable`` is more deterministic.
        import sys
        return [sys.executable, str(target)]
    if suffix == ".sh":
        # On Linux containers (our docker image) /bin/sh always exists.
        # On a Windows host without a shell, this will surface as a
        # FileNotFoundError that the caller logs cleanly.
        sh = shutil.which("sh") or "/bin/sh"
        return [sh, str(target)]
    raise ValueError(f"unsupported extension {suffix!r}")


async def run_pre_script(
    *,
    workspace_dir: Path,
    script_path: str,
    timeout_seconds: int,
    job_name: str = "<anonymous>",
) -> PreScriptResult:
    """Execute the pre-script and return a captured-stdout result.

    Never raises into the caller — every failure path (validation, exec,
    timeout, decode) becomes a result row with ``ok=False``. The cron
    runner uses ``error`` to populate ``CronJob.last_error`` and the agent
    loop's instruction stays well-formed regardless.

    body now routes through :class:`HostTaskRuntime` so the
    bounded-timeout / bounded-stdout / cwd-pinning ceilings live in one
    place (shared with ``code_execution`` and any future runtime
    flavour). The public surface (``PreScriptResult`` shape, error
    wording grepped by operators in ``CronJob.last_error``,
    ``_resolve_safe_path`` validation API) is preserved.
    """
    started = time.monotonic()
    timeout = max(1, min(int(timeout_seconds or 30), MAX_TIMEOUT_SECONDS))

    # Eager validation so the legacy ValueError messages still surface
    # for path-only callers (smoke imports ``_resolve_safe_path``).
    try:
        target = _resolve_safe_path(workspace_dir, script_path)
        _interpreter_for(target)  # raises early on bad suffix
    except ValueError as exc:
        return PreScriptResult(
            ok=False, stdout="", error=str(exc),
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    logger.info(
        "running pre_script for cron job '{}': {} (timeout={}s)",
        job_name, target, timeout,
    )

    runtime = HostTaskRuntime(workspace_dir)
    policy = SandboxPolicy(
        timeout_seconds=timeout,
        max_output_bytes=MAX_STDOUT_BYTES,
    )
    rr = await runtime.execute(
        kind="script",
        payload=script_path,
        policy=policy,
        labels={"job_name": job_name, "purpose": "pre_script"},
    )

    if rr.ok:
        return PreScriptResult(
            ok=True, stdout=rr.stdout, error=None,
            duration_ms=rr.duration_ms,
            truncated=rr.truncated_stdout,
        )

    # Map the runtime's failure shape to the v0.10 wording so operators
    # who already grep ``CronJob.last_error`` for "pre_script timed out"
    # / "pre_script exited with code N" don't see a regression.
    rr_err = rr.error or "pre_script failed"
    if "timed out" in rr_err:
        error = f"pre_script timed out after {timeout}s"
    elif rr.exit_code is not None and rr.exit_code != 0:
        stderr_preview = (rr.stderr or "")[:400]
        error = (
            f"pre_script exited with code {rr.exit_code};"
            f" stderr (truncated): {stderr_preview}"
        )
    else:
        error = rr_err
    return PreScriptResult(
        ok=False, stdout=rr.stdout, error=error,
        duration_ms=rr.duration_ms,
        truncated=rr.truncated_stdout,
    )
