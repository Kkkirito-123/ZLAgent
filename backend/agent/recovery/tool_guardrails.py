"""v0.20 Tool-call loop guardrail (ported simplified from Hermes Agent).

Detects three pathological patterns inside a single agent turn:

1. **Repeated exact failure** — the same ``(tool_name, args)`` signature
   has failed N times. Insert a warning, optionally block.
2. **Same tool repeated failure** — a single tool failed N times this turn
   even with different arguments (a flaky tool / wrong API). Warn after a
   higher threshold; halt at a still-higher one.
3. **Read-only no-progress** — a read-only tool returned the *same* hashed
   result M times for the same arguments. Warn / block.

The controller is **side-effect free** — it only returns
:class:`ToolGuardrailDecision` objects describing the situation. The
agent loop owns whether those decisions become guidance suffixes,
synthetic blocked results, or a turn halt.

Idempotency is detected from the registered :class:`Tool`'s metadata
(``is_read_only and is_concurrency_safe``) rather than a hard-coded
allow-list, so MCP / plugin / future tools work automatically.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolCallGuardrailConfig:
    """Thresholds for per-turn tool-call loop detection.

    Warnings are enabled by default and never prevent execution. Hard
    stops are explicit opt-in so single-shot retries don't accidentally
    trip a circuit breaker — the operator turns it on once they have
    confidence in the thresholds.
    """

    warnings_enabled: bool = True
    hard_stop_enabled: bool = False
    exact_failure_warn_after: int = 2
    exact_failure_block_after: int = 5
    same_tool_failure_warn_after: int = 3
    same_tool_failure_halt_after: int = 8
    no_progress_warn_after: int = 2
    no_progress_block_after: int = 5


# ---------------------------------------------------------------------------
# Signature + decision primitives
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolCallSignature:
    """Stable, non-reversible identity for a tool name + canonical args."""

    tool_name: str
    args_hash: str

    @classmethod
    def from_call(
        cls, tool_name: str, args: Mapping[str, Any] | None
    ) -> "ToolCallSignature":
        canonical = canonical_tool_args(args or {})
        return cls(tool_name=tool_name, args_hash=_sha256(canonical))

    def to_metadata(self) -> dict[str, str]:
        return {"tool_name": self.tool_name, "args_hash": self.args_hash}


@dataclass(frozen=True)
class ToolGuardrailDecision:
    """Verdict returned by the controller.

    ``action`` ∈ ``{allow, warn, block, halt}``:

    * ``allow`` — proceed normally; no message.
    * ``warn``  — proceed but append :attr:`message` to the tool result so
                   the LLM sees the loop-detection hint.
    * ``block`` — refuse execution; emit a synthetic tool result.
    * ``halt``  — finalise the turn after the current step.
    """

    action: str = "allow"
    code: str = "allow"
    message: str = ""
    tool_name: str = ""
    count: int = 0
    signature: Optional[ToolCallSignature] = None

    @property
    def allows_execution(self) -> bool:
        return self.action in {"allow", "warn"}

    @property
    def should_halt(self) -> bool:
        return self.action in {"block", "halt"}

    def to_metadata(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "action": self.action,
            "code": self.code,
            "message": self.message,
            "tool_name": self.tool_name,
            "count": self.count,
        }
        if self.signature is not None:
            data["signature"] = self.signature.to_metadata()
        return data


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------


class ToolCallGuardrailController:
    """Per-turn controller for repeated failed / non-progressing calls.

    Reset between turns (at the top of every ``_run_tool_loop``); the
    counters are deliberately scoped to a single turn so a flaky tool's
    history doesn't leak across user requests.
    """

    def __init__(self, config: Optional[ToolCallGuardrailConfig] = None) -> None:
        self.config = config or ToolCallGuardrailConfig()
        self.reset_for_turn()

    # ----- lifecycle -----
    def reset_for_turn(self) -> None:
        self._exact_failure_counts: dict[ToolCallSignature, int] = {}
        self._same_tool_failure_counts: dict[str, int] = {}
        self._no_progress: dict[ToolCallSignature, tuple[str, int]] = {}
        self._halt_decision: Optional[ToolGuardrailDecision] = None

    @property
    def halt_decision(self) -> Optional[ToolGuardrailDecision]:
        return self._halt_decision

    # ----- before / after hooks -----
    def before_call(
        self,
        tool_name: str,
        args: Mapping[str, Any] | None,
        *,
        is_read_only: bool = False,
    ) -> ToolGuardrailDecision:
        """Decide whether the call may proceed.

        With ``hard_stop_enabled=False`` (default), this always returns
        ``allow`` — the after-call pathway still records counters so
        warnings still fire on subsequent calls.
        """
        signature = ToolCallSignature.from_call(tool_name, args)
        if not self.config.hard_stop_enabled:
            return ToolGuardrailDecision(tool_name=tool_name, signature=signature)

        exact_count = self._exact_failure_counts.get(signature, 0)
        if exact_count >= self.config.exact_failure_block_after:
            decision = ToolGuardrailDecision(
                action="block",
                code="repeated_exact_failure_block",
                message=(
                    f"Blocked {tool_name}: identical args have failed"
                    f" {exact_count} times this turn. Stop retrying it"
                    " unchanged; change strategy or explain the blocker."
                ),
                tool_name=tool_name,
                count=exact_count,
                signature=signature,
            )
            self._halt_decision = decision
            return decision

        if is_read_only:
            record = self._no_progress.get(signature)
            if record is not None:
                _, repeat_count = record
                if repeat_count >= self.config.no_progress_block_after:
                    decision = ToolGuardrailDecision(
                        action="block",
                        code="idempotent_no_progress_block",
                        message=(
                            f"Blocked {tool_name}: this read-only call returned"
                            f" the same result {repeat_count} times. Stop"
                            " repeating it unchanged; reuse the existing result"
                            " or try a different query."
                        ),
                        tool_name=tool_name,
                        count=repeat_count,
                        signature=signature,
                    )
                    self._halt_decision = decision
                    return decision

        return ToolGuardrailDecision(tool_name=tool_name, signature=signature)

    def after_call(
        self,
        tool_name: str,
        args: Mapping[str, Any] | None,
        result_content: str | None,
        *,
        failed: bool,
        is_read_only: bool = False,
    ) -> ToolGuardrailDecision:
        signature = ToolCallSignature.from_call(tool_name, args)

        if failed:
            exact_count = self._exact_failure_counts.get(signature, 0) + 1
            self._exact_failure_counts[signature] = exact_count
            self._no_progress.pop(signature, None)

            same_count = self._same_tool_failure_counts.get(tool_name, 0) + 1
            self._same_tool_failure_counts[tool_name] = same_count

            if (
                self.config.hard_stop_enabled
                and same_count >= self.config.same_tool_failure_halt_after
            ):
                decision = ToolGuardrailDecision(
                    action="halt",
                    code="same_tool_failure_halt",
                    message=(
                        f"Stopped {tool_name}: it failed {same_count} times this"
                        " turn. Stop retrying the same failing path and choose"
                        " a different approach."
                    ),
                    tool_name=tool_name,
                    count=same_count,
                    signature=signature,
                )
                self._halt_decision = decision
                return decision

            if (
                self.config.warnings_enabled
                and exact_count >= self.config.exact_failure_warn_after
            ):
                return ToolGuardrailDecision(
                    action="warn",
                    code="repeated_exact_failure_warning",
                    message=(
                        f"{tool_name} has failed {exact_count} times with"
                        " identical arguments. This looks like a loop —"
                        " inspect the error and change strategy instead of"
                        " retrying it unchanged."
                    ),
                    tool_name=tool_name,
                    count=exact_count,
                    signature=signature,
                )

            if (
                self.config.warnings_enabled
                and same_count >= self.config.same_tool_failure_warn_after
            ):
                return ToolGuardrailDecision(
                    action="warn",
                    code="same_tool_failure_warning",
                    message=(
                        f"{tool_name} has failed {same_count} times this turn."
                        " This looks like a loop; change approach before"
                        " retrying."
                    ),
                    tool_name=tool_name,
                    count=same_count,
                    signature=signature,
                )

            return ToolGuardrailDecision(
                tool_name=tool_name, count=exact_count, signature=signature
            )

        # Success path: clear failure counters, then check no-progress for
        # read-only tools.
        self._exact_failure_counts.pop(signature, None)
        self._same_tool_failure_counts.pop(tool_name, None)

        if not is_read_only:
            self._no_progress.pop(signature, None)
            return ToolGuardrailDecision(
                tool_name=tool_name, signature=signature
            )

        result_hash = _sha256_text(result_content or "")
        previous = self._no_progress.get(signature)
        repeat_count = 1
        if previous is not None and previous[0] == result_hash:
            repeat_count = previous[1] + 1
        self._no_progress[signature] = (result_hash, repeat_count)

        if (
            self.config.warnings_enabled
            and repeat_count >= self.config.no_progress_warn_after
        ):
            return ToolGuardrailDecision(
                action="warn",
                code="idempotent_no_progress_warning",
                message=(
                    f"{tool_name} returned the same result {repeat_count} times."
                    " Reuse the existing result or change the query rather"
                    " than repeating the same call."
                ),
                tool_name=tool_name,
                count=repeat_count,
                signature=signature,
            )

        return ToolGuardrailDecision(
            tool_name=tool_name, count=repeat_count, signature=signature
        )


# ---------------------------------------------------------------------------
# Render helpers
# ---------------------------------------------------------------------------


def toolguard_synthetic_result(decision: ToolGuardrailDecision) -> str:
    """Build a JSON ``role=tool`` content for a blocked call."""
    return json.dumps(
        {
            "error": decision.message or "tool call blocked by guardrail",
            "guardrail": decision.to_metadata(),
        },
        ensure_ascii=False,
    )


def append_toolguard_guidance(
    result_content: str, decision: ToolGuardrailDecision
) -> str:
    """Append warn/halt guidance to the current tool result content."""
    if decision.action not in {"warn", "halt"} or not decision.message:
        return result_content
    label = (
        "Tool loop hard stop" if decision.action == "halt" else "Tool loop warning"
    )
    suffix = (
        f"\n\n[{label}: code={decision.code}; count={decision.count};"
        f" {decision.message}]"
    )
    return (result_content or "") + suffix


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def canonical_tool_args(args: Mapping[str, Any]) -> str:
    """Return sorted compact JSON for the given args mapping."""
    if not isinstance(args, Mapping):
        raise TypeError(
            f"tool args must be a mapping, got {type(args).__name__}"
        )
    return json.dumps(
        args,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_text(value: str) -> str:
    """Hash *just* the text. Tries to canonicalise JSON first."""
    if not value:
        return _sha256("")
    try:
        parsed = json.loads(value)
        canonical = json.dumps(
            parsed,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    except (TypeError, ValueError):
        canonical = value
    return _sha256(canonical)
