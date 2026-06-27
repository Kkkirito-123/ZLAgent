"""Sandbox policy primitives.

A :class:`SandboxPolicy` is a plain settings bag the runtime consults
before *and* during execution: timeout, max output, allowed working
directories, network policy hint, allow/deny tool lists. The bag does
not by itself enforce anything; runtimes call :func:`check_path` and
their own check helpers to apply each rule.

Defaults match the existing v0.10 ``pre_script`` / v0.13
``code_execution`` semantics so v0.25.1 can swap them onto the runtime
without changing observable behaviour.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


# Hard global ceilings — even an explicit `SandboxPolicy(timeout_seconds=99999)`
# is clamped to these in the runtime. They mirror v0.10/v0.13 caps so we
# don't accidentally widen the attack surface by introducing the runtime.
GLOBAL_TIMEOUT_CEILING_SECONDS = 300
GLOBAL_OUTPUT_CEILING_BYTES = 256 * 1024


class PolicyViolation(ValueError):
    """Raised when a runtime is asked to do something the policy forbids."""


@dataclass(slots=True, frozen=True)
class SandboxPolicy:
    """Per-call execution constraints.

    Construction order matters surprisingly little — every constraint
    is checked independently. The intent is: pass the same policy
    instance to multiple ``runtime.execute`` calls in a row, override
    a single field via :meth:`with_overrides` when one specific call
    needs to differ.
    """

    timeout_seconds: int = 30
    max_output_bytes: int = 64 * 1024
    allow_network: bool = True
    # Paths the script may write to. Empty tuple = enforced read-only
    # (the runtime caller decides whether to actually enforce it; v0.25
    # records the intent without filesystem-level locking, since real
    # path locking needs a docker/seccomp later).
    writable_paths: tuple[str, ...] = ()
    # Tool name allow / deny lists. Used by future agent integrations
    # (a ``TaskRuntime`` doesn't know about tools directly, but the
    # caller can pre-flight against this).
    allowed_tools: tuple[str, ...] = ()
    denied_tools: tuple[str, ...] = ()
    # Free-form labels for audit log lines.
    labels: dict[str, str] = field(default_factory=dict)

    # ----- helpers -----

    @property
    def effective_timeout_seconds(self) -> int:
        return max(1, min(GLOBAL_TIMEOUT_CEILING_SECONDS, int(self.timeout_seconds)))

    @property
    def effective_max_output_bytes(self) -> int:
        return max(1024, min(GLOBAL_OUTPUT_CEILING_BYTES, int(self.max_output_bytes)))

    def with_overrides(self, **changes) -> "SandboxPolicy":
        """Return a copy of self with fields replaced by ``changes``."""
        from dataclasses import replace
        return replace(self, **changes)

    def is_tool_allowed(self, tool_name: str) -> bool:
        if tool_name in self.denied_tools:
            return False
        if self.allowed_tools and tool_name not in self.allowed_tools:
            return False
        return True

    def to_dict(self) -> dict:
        return {
            "timeout_seconds": self.effective_timeout_seconds,
            "max_output_bytes": self.effective_max_output_bytes,
            "allow_network": self.allow_network,
            "writable_paths": list(self.writable_paths),
            "allowed_tools": list(self.allowed_tools),
            "denied_tools": list(self.denied_tools),
            "labels": dict(self.labels),
        }


DEFAULT_POLICY = SandboxPolicy()


def check_path_under(workspace_dir: Path, raw: str) -> Path:
    """Resolve ``raw`` and ensure it stays under ``workspace_dir``.

    Mirrors ``pre_script._resolve_safe_path`` minus the existence check —
    the runtime allows the caller to verify existence separately so
    create-then-execute flows work.
    """
    if not raw or not str(raw).strip():
        raise PolicyViolation("path is empty")
    candidate = Path(str(raw).strip())
    if candidate.is_absolute():
        raise PolicyViolation("path must be a workspace-relative path")
    workspace_resolved = workspace_dir.resolve()
    target = (workspace_resolved / candidate).resolve()
    try:
        target.relative_to(workspace_resolved)
    except ValueError as exc:
        raise PolicyViolation(
            f"path {raw!r} escapes workspace_dir"
        ) from exc
    return target


def writable_paths_match(
    target: Path, allowed: Iterable[str], workspace_dir: Path,
) -> bool:
    """Return True if ``target`` lies under one of ``allowed`` (workspace-relative)."""
    try:
        target_resolved = target.resolve()
    except OSError:
        return False
    workspace_resolved = workspace_dir.resolve()
    for entry in allowed:
        candidate = (workspace_resolved / Path(entry)).resolve()
        try:
            target_resolved.relative_to(candidate)
            return True
        except ValueError:
            continue
    return False


__all__ = [
    "DEFAULT_POLICY",
    "GLOBAL_OUTPUT_CEILING_BYTES",
    "GLOBAL_TIMEOUT_CEILING_SECONDS",
    "PolicyViolation",
    "SandboxPolicy",
    "check_path_under",
    "writable_paths_match",
]
