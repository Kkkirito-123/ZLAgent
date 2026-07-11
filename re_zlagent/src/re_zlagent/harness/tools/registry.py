"""Tool registry for the re_zlagent harness."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from .base import Tool, ToolExecutionContext, ToolPermission, ToolResult
from .metadata import RecommendedNextAction, SideEffect, ToolErrorType
from .permission import PermissionDecision, PermissionPolicy


@dataclass(frozen=True, slots=True)
class ToolSearchResult:
    """Read-only tool discovery result."""

    name: str
    description: str
    permission: str
    score: int
    is_read_only: bool
    is_concurrency_safe: bool
    is_destructive: bool
    side_effects: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "permission": self.permission,
            "score": self.score,
            "is_read_only": self.is_read_only,
            "is_concurrency_safe": self.is_concurrency_safe,
            "is_destructive": self.is_destructive,
            "side_effects": list(self.side_effects),
        }


@dataclass(frozen=True, slots=True)
class PreparedToolCall:
    """Permission-checked call with deterministic external intents."""

    tool_name: str
    arguments: dict[str, Any]
    context: ToolExecutionContext
    side_effect_intents: tuple[SideEffect, ...]
    outbox_required: bool
    side_effect_retry_safe: bool
    _registry_token: object = field(repr=False, compare=False)
    rejection: ToolResult | None = None
    tool: Tool | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", dict(self.arguments))
        object.__setattr__(self, "side_effect_intents", tuple(self.side_effect_intents))

    @property
    def ready(self) -> bool:
        return self.rejection is None and self.tool is not None


class ToolRegistry:
    """Register tools, expose schemas, and execute through permission policy."""

    def __init__(self, *, permission_policy: PermissionPolicy | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        self._permission_policy = permission_policy or PermissionPolicy()
        self._preparation_token = object()

    def register(self, tool: Tool) -> None:
        if not tool.name:
            raise ValueError("tool.name must be non-empty")
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool name: {tool.name}")
        if tool.permission is ToolPermission.DENY:
            return
        if tool.side_effects and not tool.outbox_required:
            raise ValueError(
                f"side-effect tool must require outbox: {tool.name}"
            )
        if tool.outbox_required and not tool.side_effects:
            raise ValueError(
                f"outbox-required tool must declare side-effect types: {tool.name}"
            )
        if tool.outbox_required and tool.is_read_only:
            raise ValueError(
                f"outbox-required tool cannot be read-only: {tool.name}"
            )
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def search(
        self,
        query: str,
        *,
        limit: int = 8,
        include_confirm: bool = False,
    ) -> tuple[ToolSearchResult, ...]:
        """Return bounded read-only tool discovery results."""

        terms = [part for part in query.lower().replace("_", " ").split() if part]
        scored: list[ToolSearchResult] = []
        for tool in self._tools.values():
            if tool.permission is ToolPermission.CONFIRM and not include_confirm:
                continue
            fields = (
                tool.name,
                tool.name.replace("_", " "),
                tool.description,
                tool.permission.value,
                " ".join(tool.side_effects),
            )
            haystack = " ".join(fields).lower()
            score = 1 if not terms else 0
            for term in terms:
                if term == tool.name.lower():
                    score += 20
                elif tool.name.lower().startswith(term):
                    score += 12
                elif term in haystack:
                    score += 5
            if score <= 0:
                continue
            scored.append(
                ToolSearchResult(
                    name=tool.name,
                    description=tool.description,
                    permission=tool.permission.value,
                    score=score,
                    is_read_only=tool.is_read_only,
                    is_concurrency_safe=tool.is_concurrency_safe,
                    is_destructive=tool.is_destructive,
                    side_effects=tool.side_effects,
                )
            )
        scored.sort(key=lambda item: (-item.score, item.name))
        return tuple(scored[: max(1, limit)])

    def __len__(self) -> int:
        return len(self._tools)

    def to_openai_schema(
        self,
        *,
        include_confirm: bool = False,
        whitelist: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        schemas: list[dict[str, Any]] = []
        for tool in self._tools.values():
            if whitelist is not None and tool.name not in whitelist:
                continue
            if tool.permission is ToolPermission.CONFIRM and not include_confirm:
                continue
            schemas.append({
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.input_schema,
                },
            })
        return schemas

    def to_planning_schema(
        self,
        *,
        include_confirm: bool = False,
        whitelist: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Expose model-facing capability schemas without execution authority."""

        schemas: list[dict[str, Any]] = []
        for name in sorted(self._tools):
            tool = self._tools[name]
            if whitelist is not None and tool.name not in whitelist:
                continue
            if tool.permission is ToolPermission.CONFIRM and not include_confirm:
                continue
            schemas.append({
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
                "permission": tool.permission.value,
                "confirmation_required": tool.permission is ToolPermission.CONFIRM,
                "is_read_only": tool.is_read_only,
                "is_concurrency_safe": tool.is_concurrency_safe,
                "is_destructive": tool.is_destructive,
                "side_effects": list(tool.side_effects),
                "outbox_required": tool.outbox_required,
            })
        return schemas

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        interactive: bool = True,
        allow_confirm: bool = False,
        idempotency_key: str | None = None,
    ) -> ToolResult:
        prepared = self.prepare(
            name,
            arguments,
            interactive=interactive,
            allow_confirm=allow_confirm,
            idempotency_key=idempotency_key,
        )
        return await self.execute_prepared(prepared)

    def prepare(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        interactive: bool = True,
        allow_confirm: bool = False,
        idempotency_key: str | None = None,
    ) -> PreparedToolCall:
        """Check permission and derive intents without dispatching the tool."""

        normalized_arguments = dict(arguments or {})
        invocation_key = idempotency_key or self._direct_idempotency_key(
            name,
            normalized_arguments,
        )
        tool = self.get(name)
        permission = self._permission_policy.decide_tool_call(
            tool=tool,
            tool_name=name,
            arguments=normalized_arguments,
            interactive=interactive,
            trust_confirm_tools=allow_confirm,
        )
        if permission.decision is PermissionDecision.DENY:
            return self._rejected_preparation(
                name,
                normalized_arguments,
                invocation_key,
                ToolResult.denied(permission.reason),
            )
        if permission.decision is PermissionDecision.CONFIRM:
            return self._rejected_preparation(
                name,
                normalized_arguments,
                invocation_key,
                ToolResult.requires_confirmation(permission.reason),
            )

        assert tool is not None
        try:
            intents = tuple(tool.plan_side_effects(normalized_arguments))
        except Exception as exc:  # noqa: BLE001 - preparation failures are data
            return self._rejected_preparation(
                name,
                normalized_arguments,
                invocation_key,
                ToolResult.failure(
                    f"side-effect planning failed: {type(exc).__name__}: {exc}",
                    error_type=ToolErrorType.INVALID_INPUT,
                    recoverable_by_model=True,
                    recommended_next_action=RecommendedNextAction.RETRY,
                    source="tool_preparation",
                ),
            )
        if tool.outbox_required and not intents:
            return self._rejected_preparation(
                name,
                normalized_arguments,
                invocation_key,
                ToolResult.failure(
                    "outbox-required tool declared no side-effect intent",
                    error_type=ToolErrorType.UNSAFE_WRITE,
                    recoverable_by_model=False,
                    recommended_next_action=RecommendedNextAction.MANUAL_REVIEW,
                    source="tool_preparation",
                ),
            )
        side_effect_keys = tuple(
            f"{invocation_key}:side_effect:{index}"
            for index, _ in enumerate(intents)
        )
        return PreparedToolCall(
            tool_name=name,
            arguments=normalized_arguments,
            context=ToolExecutionContext(
                idempotency_key=invocation_key,
                side_effect_keys=side_effect_keys,
            ),
            side_effect_intents=intents,
            outbox_required=tool.outbox_required,
            side_effect_retry_safe=tool.side_effect_retry_safe,
            _registry_token=self._preparation_token,
            tool=tool,
        )

    async def execute_prepared(self, prepared: PreparedToolCall) -> ToolResult:
        """Dispatch a prepared call without repeating permission checks."""

        if prepared._registry_token is not self._preparation_token:
            return ToolResult.denied(
                "prepared tool call does not belong to this registry"
            )
        if prepared.rejection is not None:
            return prepared.rejection
        tool = prepared.tool
        if tool is None:
            return ToolResult.failure(
                "prepared tool call has no tool",
                error_type=ToolErrorType.TOOL_EXCEPTION,
                recoverable_by_model=False,
                recommended_next_action=RecommendedNextAction.MANUAL_REVIEW,
                source="tool_preparation",
            )
        try:
            return await tool.execute_with_context(
                prepared.arguments,
                prepared.context,
            )
        except Exception as exc:  # noqa: BLE001 - tool exceptions are data
            return ToolResult.failure(
                f"{type(exc).__name__}: {exc}",
                error_type=ToolErrorType.TOOL_EXCEPTION,
                recoverable_by_model=False,
                recommended_next_action=RecommendedNextAction.MANUAL_REVIEW,
                raw={"tool_name": prepared.tool_name},
            )

    @staticmethod
    def _direct_idempotency_key(name: str, arguments: dict[str, Any]) -> str:
        canonical = json.dumps(
            {"tool_name": name, "arguments": arguments},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            default=str,
        ).encode("utf-8")
        return f"direct:{name}:{hashlib.sha256(canonical).hexdigest()}"

    def _rejected_preparation(
        self,
        name: str,
        arguments: dict[str, Any],
        idempotency_key: str,
        rejection: ToolResult,
    ) -> PreparedToolCall:
        return PreparedToolCall(
            tool_name=name,
            arguments=arguments,
            context=ToolExecutionContext(idempotency_key=idempotency_key),
            side_effect_intents=(),
            outbox_required=False,
            side_effect_retry_safe=False,
            _registry_token=self._preparation_token,
            rejection=rejection,
        )
