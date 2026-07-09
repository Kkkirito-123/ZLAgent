"""Tool registry for the re_zlagent harness."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .base import Tool, ToolPermission, ToolResult
from .metadata import RecommendedNextAction, ToolErrorType
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


class ToolRegistry:
    """Register tools, expose schemas, and execute through permission policy."""

    def __init__(self, *, permission_policy: PermissionPolicy | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        self._permission_policy = permission_policy or PermissionPolicy()

    def register(self, tool: Tool) -> None:
        if not tool.name:
            raise ValueError("tool.name must be non-empty")
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool name: {tool.name}")
        if tool.permission is ToolPermission.DENY:
            return
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

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        interactive: bool = True,
        allow_confirm: bool = False,
    ) -> ToolResult:
        tool = self.get(name)
        permission = self._permission_policy.decide_tool_call(
            tool=tool,
            tool_name=name,
            arguments=arguments or {},
            interactive=interactive,
            trust_confirm_tools=allow_confirm,
        )
        if permission.decision is PermissionDecision.DENY:
            return ToolResult.denied(permission.reason)
        if permission.decision is PermissionDecision.CONFIRM:
            return ToolResult.requires_confirmation(permission.reason)

        assert tool is not None
        try:
            return await tool.execute(arguments or {})
        except Exception as exc:  # noqa: BLE001 - tool exceptions are data
            return ToolResult.failure(
                f"{type(exc).__name__}: {exc}",
                error_type=ToolErrorType.TOOL_EXCEPTION,
                recoverable_by_model=False,
                recommended_next_action=RecommendedNextAction.MANUAL_REVIEW,
                raw={"tool_name": name},
            )
