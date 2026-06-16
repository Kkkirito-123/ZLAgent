"""Abstract :class:`TaskRuntime` and :class:`RuntimeResult`.

Common interface for executing scripts / inline snippets under a
:class:`SandboxPolicy`. v0.25 ships only the host (in-process)
implementation; future flavours (docker, e2b, firecracker) plug into
the same protocol so the call sites in ``pre_script`` / ``code_execution``
stay unchanged when the sandbox flavour rotates.

Interface intentionally narrow: ``execute(kind, payload, policy, ...)``
returns a :class:`RuntimeResult` and never raises into the caller —
every failure path (policy violation, missing interpreter, timeout,
process crash) becomes ``ok=False`` with an ``error`` string. Callers
are free to ignore stdout / stderr / exit_code when they only need the
yes/no answer.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from .policy import SandboxPolicy


class RuntimeKind(str, Enum):
    """Identifies the concrete runtime flavour."""

    HOST = "host"        # in-process subprocess on the same OS
    DOCKER = "docker"    # ephemeral container (deferred)
    E2B = "e2b"          # remote sandbox (deferred)


@dataclass(slots=True)
class RuntimeResult:
    """Uniform result row returned by every :class:`TaskRuntime`.

    ``stdout`` / ``stderr`` are already truncated to the policy's
    ``max_output_bytes``. ``truncated_*`` flags signal whether the
    truncation happened. ``duration_ms`` is wall-clock and includes
    process startup so callers comparing flavours see apples-to-apples
    timings.

    ``extra`` is a free-form dict for runtime-specific telemetry
    (argv, container id, sandbox session id, …) that callers can log
    but should not depend on for control flow.
    """

    ok: bool = False
    stdout: str = ""
    stderr: str = ""
    error: Optional[str] = None
    exit_code: Optional[int] = None
    duration_ms: int = 0
    truncated_stdout: bool = False
    truncated_stderr: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": bool(self.ok),
            "stdout": self.stdout,
            "stderr": self.stderr,
            "error": self.error,
            "exit_code": self.exit_code,
            "duration_ms": int(self.duration_ms),
            "truncated_stdout": bool(self.truncated_stdout),
            "truncated_stderr": bool(self.truncated_stderr),
            "extra": dict(self.extra),
        }


class TaskRuntime(abc.ABC):
    """Abstract execution backend for code snippets / scripts.

    Concrete classes set ``name`` and ``kind``. The ``execute`` method
    must accept ``kind="script" | "snippet"``; ``script`` resolves a
    workspace-relative path while ``snippet`` materialises ``payload``
    into a temp file before running it. Both paths apply the same
    :class:`SandboxPolicy` ceilings.
    """

    #: Short identifier used in log lines / telemetry.
    name: str = "abstract"
    #: Coarse runtime flavour for /api/info exposure.
    kind: RuntimeKind = RuntimeKind.HOST

    @abc.abstractmethod
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
        """Run ``payload`` under ``policy`` and return a :class:`RuntimeResult`.

        ``kind``:
          * ``"script"``  — ``payload`` is a workspace-relative path
            (e.g. ``"scripts/digest.py"``).
          * ``"snippet"`` — ``payload`` is the full source to execute.

        ``env`` is merged with the host env by the runtime; concrete
        implementations decide how aggressive the merge is. ``cwd``
        is workspace-relative; an absolute path raises a policy
        violation.

        ``labels`` are surfaced in :attr:`RuntimeResult.extra` so audit
        logs can correlate the result with the calling tool / cron job.
        """
        raise NotImplementedError


__all__ = [
    "RuntimeKind",
    "RuntimeResult",
    "TaskRuntime",
]
