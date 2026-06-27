"""ToolRegistry: the single source of truth for tools the agent can invoke."""
from __future__ import annotations

from typing import Any, Iterable, Optional

from loguru import logger

from .base import Tool, ToolPermission, ToolResult


class ToolRegistry:
    """Registers tools, exposes their OpenAI schema, and brokers execution.

    The registry is intentionally permissive about registration (so new tools
    can be added without touching this class) but strict about execution:

    * ``DENY``-tier tools are dropped at register time — they cannot reach
      the agent loop at all.
    * ``CONFIRM``-tier tools are registered but are hidden from the LLM
      schema by default (``include_confirm=False``). Attempting to execute
      one returns an error until v0.6 ships the confirmation broker.
    * ``SAFE``-tier tools execute immediately; exceptions become
      ``ToolResult(ok=False, ...)`` so a single buggy tool cannot crash
      the agent loop.
    """

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    # -- registration --------------------------------------------------

    def register(self, tool: Tool) -> None:
        if not tool.name:
            raise ValueError("tool.name must be a non-empty string")
        if tool.permission is ToolPermission.DENY:
            logger.warning("tool '{}' is DENY-listed; skipping registration", tool.name)
            return
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool name: {tool.name}")
        self._tools[tool.name] = tool
        logger.info("tool registered: {} ({})", tool.name, tool.permission.value)

    def register_many(self, tools: Iterable[Tool]) -> None:
        for t in tools:
            self.register(t)

    def unregister(self, name: str) -> bool:
        """Drop a previously-registered tool from the registry.

        Returns True if the tool existed and was removed, False if no such
        name was registered. v0.33 added this so :class:`MCPManageTool`
        can hot-detach an MCP server's tools at runtime when the operator
        removes the server through IM.
        """
        existed = name in self._tools
        if existed:
            del self._tools[name]
            logger.info("tool unregistered: {}", name)
        return existed

    def unregister_prefix(self, prefix: str) -> list[str]:
        """Drop every tool whose name starts with ``prefix``.

        Used by :class:`MCPManageTool` to detach all ``mcp__<server>__*``
        wrappers in one shot when removing a server. Returns the list of
        names that were actually removed (empty if no match).
        """
        if not prefix:
            return []
        victims = [n for n in self._tools if n.startswith(prefix)]
        for n in victims:
            del self._tools[n]
        if victims:
            logger.info(
                "tool registry unregistered {} entries for prefix {!r}",
                len(victims), prefix,
            )
        return victims

    # -- lookup --------------------------------------------------------

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def list(self) -> list[Tool]:
        return list(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)

    def counts_by_permission(self) -> dict[str, int]:
        out: dict[str, int] = {p.value: 0 for p in ToolPermission}
        for tool in self._tools.values():
            out[tool.permission.value] += 1
        return out

    # -- schema export -------------------------------------------------

    def to_openai_schema(
        self,
        *,
        include_confirm: bool = False,
        whitelist: Optional[set[str]] = None,
        include_deferred: bool = False,
        force_include: Optional[set[str]] = None,
    ) -> list[dict[str, Any]]:
        """Render the registry as an OpenAI ``tools`` array.

        Matches the Chat Completions ``tools`` parameter spec: each entry is
        ``{"type": "function", "function": {name, description, parameters}}``.
        The same schema is accepted by DeepSeek, Moonshot, OpenRouter, Qwen,
        Together and vLLM, so no provider-specific branching is needed.

        ``whitelist`` is an optional set of tool names — when provided, *only*
        those tools are exposed (used by the v0.9 skill-review turn to
        narrow the agent's focus to ``skill_manage``).
        """
        schema: list[dict[str, Any]] = []
        force_include = force_include or set()
        for tool in self._tools.values():
            if whitelist is not None and tool.name not in whitelist:
                continue
            if (
                tool.should_defer
                and not tool.always_load
                and not include_deferred
                and tool.name not in force_include
            ):
                continue
            if tool.permission is ToolPermission.CONFIRM and not include_confirm:
                continue
            schema.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters_schema,
                    },
                }
            )
        return schema

    def search(
        self,
        query: str,
        *,
        limit: int = 8,
        include_description: bool = True,
    ) -> list[dict[str, Any]]:
        terms = [t for t in query.lower().replace("_", " ").split() if t]
        scored: list[tuple[int, str, Tool]] = []
        for tool in self._tools.values():
            fields = [
                tool.name,
                tool.name.replace("_", " "),
                tool.search_hint,
                tool.permission.value,
            ]
            if include_description:
                fields.append(tool.description)
            haystack = " ".join(fields).lower()
            score = 0
            for term in terms:
                if term == tool.name.lower():
                    score += 20
                elif tool.name.lower().startswith(term):
                    score += 12
                elif term in haystack:
                    score += 5
            if not terms:
                score = 1
            if score:
                scored.append((score, tool.name, tool))
        scored.sort(key=lambda item: (-item[0], item[1]))
        out: list[dict[str, Any]] = []
        for _, _, tool in scored[: max(1, limit)]:
            out.append(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "permission": tool.permission.value,
                    "deferred": bool(tool.should_defer and not tool.always_load),
                    "search_hint": tool.search_hint,
                    "is_read_only": tool.is_read_only,
                    "is_concurrency_safe": tool.is_concurrency_safe,
                    "is_destructive": tool.is_destructive,
                }
            )
        return out

    # -- execution -----------------------------------------------------

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any] | None,
        *,
        allow_confirm: bool = False,
    ) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(ok=False, content="", error=f"unknown tool: {name}")
        if tool.permission is ToolPermission.DENY:  # defensive; shouldn't happen
            return ToolResult(ok=False, content="", error=f"tool denied: {name}")
        if (
            tool.permission is ToolPermission.CONFIRM
            and not allow_confirm
            and not tool.is_action_read_only(arguments or {})
        ):
            return ToolResult(
                ok=False,
                content="",
                error=(
                    f"tool '{name}' requires user confirmation which is not implemented yet"
                    " (v0.6). Pick a safe tool or ask the user to approve manually."
                ),
            )
        try:
            return await tool.execute(arguments or {})
        except Exception as exc:  # noqa: BLE001 - keep agent loop alive
            logger.exception("tool '{}' raised during execute", name)
            return ToolResult(ok=False, content="", error=f"{type(exc).__name__}: {exc}")
