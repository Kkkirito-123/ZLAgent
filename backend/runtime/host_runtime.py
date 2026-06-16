"""Concrete :class:`HostTaskRuntime` — runs scripts / snippets in-process.

Mirrors the v0.10 ``pre_script`` runner's hard caps: bounded timeout,
bounded stdout, extension allowlist, workspace-relative path, no
shell expansion. A snippet path is wrapped in a temporary file before
execution so the same path-validation rules can apply uniformly.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

from loguru import logger

from .base import RuntimeKind, RuntimeResult, TaskRuntime
from .policy import (
    DEFAULT_POLICY,
    PolicyViolation,
    SandboxPolicy,
    check_path_under,
)


_ALLOWED_SCRIPT_SUFFIXES = (".py", ".sh")


class HostTaskRuntime(TaskRuntime):
    """Runs scripts / inline snippets in the same OS as ZLAgent.

    Constructor pins the workspace root once; every execution
    re-validates the path against it so swapping ``workspace_dir``
    later requires constructing a new runtime (deliberate).
    """

    name = "host"
    kind = RuntimeKind.HOST

    def __init__(self, workspace_dir: Path) -> None:
        self._workspace_dir = Path(workspace_dir).resolve()

    @property
    def workspace_dir(self) -> Path:
        return self._workspace_dir

    async def execute(
        self,
        *,
        kind: str,
        payload: str,
        policy: Optional[SandboxPolicy] = None,
        env: Optional[dict[str, str]] = None,
        cwd: Optional[str] = None,
        labels: Optional[dict[str, str]] = None,
    ) -> RuntimeResult:
        pol = policy or DEFAULT_POLICY
        labels = dict(labels or {})

        # Resolve the on-disk path from kind/payload ----------------------
        cleanup_path: Optional[Path] = None
        try:
            if kind == "script":
                target = check_path_under(self._workspace_dir, payload)
                if not target.exists():
                    return RuntimeResult(
                        ok=False,
                        error=f"script not found: {payload}",
                        exit_code=None,
                    )
                if not target.is_file():
                    return RuntimeResult(
                        ok=False, error=f"not a regular file: {payload}",
                    )
                if target.suffix.lower() not in _ALLOWED_SCRIPT_SUFFIXES:
                    return RuntimeResult(
                        ok=False,
                        error=(
                            f"script suffix {target.suffix!r} not in "
                            f"{_ALLOWED_SCRIPT_SUFFIXES}"
                        ),
                    )
            elif kind == "snippet":
                # Materialise to a tempfile inside the workspace so the
                # policy ceiling on writable paths still applies.
                fd, tmp_str = tempfile.mkstemp(
                    suffix=".py",
                    prefix="_zlagent_snippet_",
                    dir=str(self._workspace_dir),
                    text=True,
                )
                target = Path(tmp_str)
                cleanup_path = target
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(payload)
            else:
                return RuntimeResult(
                    ok=False, error=f"unsupported kind: {kind!r}",
                )
        except PolicyViolation as exc:
            return RuntimeResult(ok=False, error=f"policy: {exc}")
        except Exception as exc:  # noqa: BLE001
            return RuntimeResult(ok=False, error=f"setup: {type(exc).__name__}: {exc}")

        argv = self._argv_for(target)
        timeout = pol.effective_timeout_seconds
        max_bytes = pol.effective_max_output_bytes
        run_env = self._merged_env(env)
        run_cwd = str(self._resolve_cwd(cwd))

        started = time.monotonic()
        proc: Optional[asyncio.subprocess.Process] = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=run_env,
                cwd=run_cwd,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout,
                )
            except asyncio.TimeoutError:
                # Best-effort terminate then kill.
                with _suppress():
                    proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    with _suppress():
                        proc.kill()
                    await proc.wait()
                return RuntimeResult(
                    ok=False,
                    error=f"timed out after {timeout}s",
                    duration_ms=int((time.monotonic() - started) * 1000),
                    exit_code=None,
                )
            duration_ms = int((time.monotonic() - started) * 1000)
            stdout, t_out = _truncate(stdout_b, max_bytes)
            stderr, t_err = _truncate(stderr_b, max_bytes)
            exit_code = proc.returncode
            return RuntimeResult(
                ok=exit_code == 0,
                stdout=stdout,
                stderr=stderr,
                error=None if exit_code == 0 else f"exit={exit_code}",
                exit_code=exit_code,
                duration_ms=duration_ms,
                truncated_stdout=t_out,
                truncated_stderr=t_err,
                extra={"argv": list(argv), "labels": labels},
            )
        except FileNotFoundError as exc:
            return RuntimeResult(
                ok=False,
                error=f"interpreter not found: {exc}",
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        except Exception as exc:  # noqa: BLE001
            return RuntimeResult(
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        finally:
            if cleanup_path is not None:
                try:
                    cleanup_path.unlink(missing_ok=True)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("snippet cleanup failed: {}", exc)

    # ------------------------------------------------------------------
    def _argv_for(self, target: Path) -> list[str]:
        suffix = target.suffix.lower()
        if suffix == ".py":
            return [sys.executable, str(target)]
        if suffix == ".sh":
            sh = shutil.which("sh") or "/bin/sh"
            return [sh, str(target)]
        # Should be unreachable thanks to the suffix check above.
        raise ValueError(f"unsupported extension {suffix!r}")

    def _merged_env(self, env: Optional[dict[str, str]]) -> dict[str, str]:
        merged = dict(os.environ)
        for key, val in (env or {}).items():
            merged[str(key)] = str(val)
        return merged

    def _resolve_cwd(self, cwd: Optional[str]) -> Path:
        if cwd is None:
            return self._workspace_dir
        target = check_path_under(self._workspace_dir, cwd)
        if not target.exists() or not target.is_dir():
            return self._workspace_dir
        return target


def _truncate(data: bytes, limit: int) -> tuple[str, bool]:
    if not data:
        return "", False
    if len(data) <= limit:
        return data.decode("utf-8", errors="replace"), False
    head = data[:limit].decode("utf-8", errors="replace")
    return head + f"\n[...truncated {len(data) - limit} bytes]", True


class _suppress:
    """Tiny context manager to swallow exceptions without importing contextlib."""

    def __enter__(self) -> "_suppress":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return True


__all__ = ["HostTaskRuntime"]
