"""Explicit tool-execution boundary for the ZLAgent product runtime."""
from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from loguru import logger

from ..core.redact import redact_mapping, redact_text
from ..tools import (
    Evidence,
    RecommendedNextAction,
    SideEffect,
    ToolErrorType,
    ToolPermission,
    ToolRegistry,
    ToolResult,
)
from .accelerate.tool_memo import ToolMemo
from .observability.tracer import ToolRecord, TraceRecorder
from .progress import ProgressEmitter


class HarnessExecution:
    """Run every tool through one observable, metadata-aware path."""

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        memo: Optional[ToolMemo] = None,
        tracer: Optional[TraceRecorder] = None,
        progress: Optional[ProgressEmitter] = None,
    ) -> None:
        self.registry = registry
        self.memo = memo
        self.tracer = tracer
        self.progress = progress

    async def execute(
        self,
        name: str,
        arguments: Optional[dict[str, Any]] = None,
        *,
        allow_confirm: bool = False,
        session_id: Optional[str] = None,
    ) -> ToolResult:
        args = dict(arguments or {})
        started = time.perf_counter()
        wall_started = time.time()
        cached = False

        await self._emit_progress(name)

        memo_key: Optional[str] = None
        if self.memo is not None and self._can_memoize(name):
            memo_key = self.memo.cache_key(name, args, session_id=session_id)
            hit = self.memo.get(memo_key)
            if hit is not None:
                result = hit
                cached = True
            else:
                result = await self.registry.execute(
                    name,
                    args,
                    allow_confirm=allow_confirm,
                )
        else:
            if self.memo is not None:
                self.memo.mark_skipped()
            result = await self.registry.execute(
                name,
                args,
                allow_confirm=allow_confirm,
            )

        result = self._normalize_result(name, args, result)
        if memo_key is not None and not cached and self.memo is not None:
            self.memo.put(memo_key, result)
        self._record_trace(
            name=name,
            arguments=args,
            result=result,
            cached=cached,
            wall_started=wall_started,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
        return result

    def _can_memoize(self, name: str) -> bool:
        if self.memo is None or not self.memo.is_memoizable(name):
            return False
        tool = self.registry.get(name)
        return bool(
            tool is not None
            and tool.permission is ToolPermission.SAFE
            and tool.is_read_only
        )

    async def _emit_progress(self, name: str) -> None:
        if self.progress is None:
            return
        try:
            await self.progress.tool_invoked(name)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("[harness.execution] progress failed for {}: {}", name, exc)

    def _normalize_result(
        self,
        name: str,
        arguments: dict[str, Any],
        result: ToolResult,
    ) -> ToolResult:
        tool = self.registry.get(name)
        if result.source == "tool":
            result.source = name
        if not result.ok and result.error_type is None:
            result.error_type = _infer_error_type(result.error)
        if not result.ok and not result.recoverable_by_model:
            result.recoverable_by_model = result.error_type in {
                ToolErrorType.INVALID_INPUT,
                ToolErrorType.NOT_FOUND,
                ToolErrorType.EXTERNAL_UNAVAILABLE,
                ToolErrorType.TIMEOUT,
                ToolErrorType.RATE_LIMITED,
                ToolErrorType.CONFLICT,
                ToolErrorType.TOOL_EXCEPTION,
            }
        if not result.ok and result.recommended_next_action is None:
            if result.error_type is ToolErrorType.PERMISSION_DENIED:
                result.recommended_next_action = RecommendedNextAction.ASK_USER
            elif result.recoverable_by_model:
                result.recommended_next_action = RecommendedNextAction.USE_ALTERNATIVE_TOOL
            else:
                result.recommended_next_action = RecommendedNextAction.STOP
        if not result.evidence:
            result.evidence = _evidence_from_raw(result.raw)
        if (
            result.ok
            and not result.side_effects
            and tool is not None
            and not tool.is_action_read_only(arguments)
        ):
            result.side_effects = (
                SideEffect(
                    type=f"{name}.mutation",
                    target=_mutation_target(name, arguments, result.raw),
                    risk="high" if tool.is_destructive else "medium",
                    metadata={"action": str(arguments.get("action") or "execute")},
                ),
            )
        return result

    def _record_trace(
        self,
        *,
        name: str,
        arguments: dict[str, Any],
        result: ToolResult,
        cached: bool,
        wall_started: float,
        elapsed_ms: int,
    ) -> None:
        if self.tracer is None:
            return
        preview = redact_text(repr(redact_mapping(arguments)))[:160]
        self.tracer.record_tool(
            ToolRecord(
                started_at=wall_started,
                name=name,
                elapsed_ms=elapsed_ms,
                ok=result.ok,
                error=redact_text(result.error or "") or None,
                args_preview=preview,
                cached=cached,
                status=result.status.value,
                source=result.source,
                evidence_count=len(result.evidence),
                side_effect_count=len(result.side_effects),
            )
        )


def _infer_error_type(error: Optional[str]) -> ToolErrorType:
    text = (error or "").lower()
    if any(token in text for token in ("permission", "denied", "requires user confirmation")):
        return ToolErrorType.PERMISSION_DENIED
    if "timed out" in text or "timeout" in text:
        return ToolErrorType.TIMEOUT
    if "rate limit" in text or "429" in text:
        return ToolErrorType.RATE_LIMITED
    if any(token in text for token in ("not found", "no such tool", "unknown tool")):
        return ToolErrorType.NOT_FOUND
    if any(token in text for token in ("required", "invalid", "must be", "unsupported")):
        return ToolErrorType.INVALID_INPUT
    if any(token in text for token in ("already exists", "conflict", "duplicate")):
        return ToolErrorType.CONFLICT
    if any(token in text for token in ("fetch failed", "http ", "dispatch failed", "unavailable")):
        return ToolErrorType.EXTERNAL_UNAVAILABLE
    return ToolErrorType.UNKNOWN


def _evidence_from_raw(raw: Optional[dict[str, Any]]) -> tuple[Evidence, ...]:
    if not raw:
        return ()
    items: list[Evidence] = []
    for key in ("source", "source_url", "url", "path"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            items.append(Evidence(type=key, ref=value.strip()))
    sources = raw.get("sources")
    if isinstance(sources, list):
        for value in sources[:20]:
            if isinstance(value, str) and value.strip():
                items.append(Evidence(type="source", ref=value.strip()))
            elif isinstance(value, dict):
                ref = value.get("url") or value.get("ref")
                if isinstance(ref, str) and ref.strip():
                    items.append(
                        Evidence(
                            type="source",
                            ref=ref.strip(),
                            summary=str(value.get("title") or ""),
                        )
                    )
    return tuple(items)


def _mutation_target(
    name: str,
    arguments: dict[str, Any],
    raw: Optional[dict[str, Any]],
) -> str:
    raw = raw or {}
    for key in (
        "path",
        "source_url",
        "delivery_target_id",
        "skill_name",
        "mode_id",
        "memory_id",
        "job_id",
    ):
        value = raw.get(key, arguments.get(key))
        if value not in (None, ""):
            return f"{key}:{value}"
    return name


__all__ = ["HarnessExecution"]
