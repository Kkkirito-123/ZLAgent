"""Dynamic Harness tool adapters for approved MCP capabilities."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Protocol

from re_zlagent.harness.tools import (
    Evidence,
    RecommendedNextAction,
    SideEffect,
    Tool,
    ToolErrorType,
    ToolExecutionContext,
    ToolPermission,
    ToolResult,
)

from .client import McpCallError, McpCallErrorCode, McpToolDescriptor


_SAFE_TOOL_PART_RE = re.compile(r"[^A-Za-z0-9_-]+")


class McpToolClient(Protocol):
    """Narrow client surface consumed by dynamic MCP tools."""

    @property
    def descriptors(self) -> tuple[McpToolDescriptor, ...]: ...

    async def call_tool(
        self,
        *,
        server_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        idempotency_key: str,
    ) -> Any: ...


class McpProxyTool(Tool):
    """Confirm-tier proxy whose concrete metadata is bound by the factory."""

    permission = ToolPermission.CONFIRM
    is_read_only = False
    is_concurrency_safe = False
    is_destructive = False
    side_effects = ("mcp",)
    outbox_required = True
    side_effect_retry_safe = False
    max_result_chars = 8_000
    interrupt_behavior = "cancel"

    def __init__(
        self,
        client: McpToolClient,
        *,
        server_id: str,
        remote_name: str,
    ) -> None:
        self._client = client
        self._server_id = server_id
        self._remote_name = remote_name

    def activity_description(self, arguments: dict[str, Any]) -> str:
        return f"Call MCP {self._server_id}/{self._remote_name}"

    def plan_side_effects(
        self,
        arguments: dict[str, Any],
    ) -> tuple[SideEffect, ...]:
        return (self._side_effect(),)

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        canonical = json.dumps(
            arguments,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            default=str,
        ).encode("utf-8")
        direct_key = (
            f"direct:{self.name}:" + hashlib.sha256(canonical).hexdigest()
        )
        return await self._execute(arguments, idempotency_key=direct_key)

    async def execute_with_context(
        self,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        idempotency_key = (
            context.side_effect_keys[0]
            if context.side_effect_keys
            else context.idempotency_key
        )
        return await self._execute(arguments, idempotency_key=idempotency_key)

    async def _execute(
        self,
        arguments: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> ToolResult:
        evidence_ref = self._evidence_ref()
        evidence_metadata = {
            "server_id": self._server_id,
            "tool_name": self._remote_name,
            "transport": "stdio",
            "idempotency_key": idempotency_key,
        }
        try:
            result = await self._client.call_tool(
                server_id=self._server_id,
                tool_name=self._remote_name,
                arguments=arguments,
                idempotency_key=idempotency_key,
            )
        except McpCallError as exc:
            error_type = (
                ToolErrorType.TIMEOUT
                if exc.code is McpCallErrorCode.TIMEOUT
                else ToolErrorType.EXTERNAL_UNAVAILABLE
            )
            return ToolResult.failure(
                str(exc),
                error_type=error_type,
                recoverable_by_model=False,
                recommended_next_action=RecommendedNextAction.MANUAL_REVIEW,
                raw={
                    "server_id": self._server_id,
                    "tool_name": self._remote_name,
                    "error_code": exc.code.value,
                    "cause_type": exc.cause_type,
                },
                evidence=[
                    Evidence(
                        type="mcp_tool_call",
                        ref=evidence_ref,
                        summary="MCP call outcome requires manual review",
                        metadata={**evidence_metadata, "status": exc.code.value},
                    )
                ],
                side_effects=[self._side_effect()],
                source=self.name,
            )

        content, truncated = _render_content(result, self.max_result_chars)
        raw = {
            "server_id": self._server_id,
            "tool_name": self._remote_name,
            "is_error": bool(result.isError),
            "content_block_types": [
                str(getattr(block, "type", "unknown")) for block in result.content
            ],
            "structured_content": _bounded_value(result.structuredContent),
            "content_truncated": truncated,
            "idempotency_key": idempotency_key,
        }
        if result.isError:
            return ToolResult.failure(
                content or "MCP server returned a tool error",
                error_type=ToolErrorType.TOOL_EXCEPTION,
                recoverable_by_model=False,
                recommended_next_action=RecommendedNextAction.MANUAL_REVIEW,
                raw=raw,
                evidence=[
                    Evidence(
                        type="mcp_tool_call",
                        ref=evidence_ref,
                        summary="MCP server returned a tool error",
                        metadata={**evidence_metadata, "status": "error"},
                    )
                ],
                side_effects=[self._side_effect()],
                source=self.name,
            )
        return ToolResult.success(
            content or "MCP tool completed without text output",
            raw=raw,
            evidence=[
                Evidence(
                    type="mcp_tool_call",
                    ref=evidence_ref,
                    summary="approved MCP tool call completed",
                    metadata={**evidence_metadata, "status": "success"},
                )
            ],
            side_effects=[self._side_effect()],
            source=self.name,
        )

    def _evidence_ref(self) -> str:
        return f"mcp://{self._server_id}/{self._remote_name}"

    def _side_effect(self) -> SideEffect:
        return SideEffect(
            type="mcp",
            target=self._evidence_ref(),
            risk="high",
            metadata={
                "server_id": self._server_id,
                "tool_name": self._remote_name,
                "transport": "stdio",
            },
        )


def create_mcp_tools(client: McpToolClient) -> tuple[Tool, ...]:
    """Bind approved remote descriptors into unique local Harness tools."""

    tools: list[Tool] = []
    names: set[str] = set()
    for descriptor in client.descriptors:
        local_name = local_mcp_tool_name(descriptor.server_id, descriptor.name)
        if local_name in names:
            raise ValueError(f"MCP tool alias collision: {local_name}")
        names.add(local_name)
        description = descriptor.description or "No description supplied by server."
        tool_type = type(
            f"McpProxy_{hashlib.sha256(local_name.encode()).hexdigest()[:12]}",
            (McpProxyTool,),
            {
                "name": local_name,
                "description": (
                    f"MCP {descriptor.server_id}/{descriptor.name}: {description}"
                ),
                "input_schema": dict(descriptor.input_schema),
            },
        )
        tools.append(
            tool_type(
                client,
                server_id=descriptor.server_id,
                remote_name=descriptor.name,
            )
        )
    return tuple(tools)


def local_mcp_tool_name(server_id: str, remote_name: str) -> str:
    """Build an OpenAI-compatible deterministic local function name."""

    server_part = _SAFE_TOOL_PART_RE.sub("_", server_id).strip("_") or "server"
    tool_part = _SAFE_TOOL_PART_RE.sub("_", remote_name).strip("_") or "tool"
    candidate = f"mcp__{server_part}__{tool_part}"
    if len(candidate) <= 64:
        return candidate
    digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:10]
    return f"{candidate[:53]}_{digest}"


def _render_content(result: Any, limit: int) -> tuple[str, bool]:
    parts: list[str] = []
    for block in result.content:
        block_type = str(getattr(block, "type", "unknown"))
        if block_type == "text":
            parts.append(str(getattr(block, "text", "")))
        elif block_type == "resource_link":
            parts.append(f"[resource_link] {getattr(block, 'uri', '')}")
        elif block_type == "resource":
            resource = getattr(block, "resource", None)
            parts.append(f"[resource] {getattr(resource, 'uri', '')}")
        else:
            parts.append(f"[{block_type} content omitted]")
    if result.structuredContent is not None:
        parts.append(
            json.dumps(
                result.structuredContent,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
        )
    content = "\n".join(part for part in parts if part)
    if len(content) <= limit:
        return content, False
    suffix = "\n[output truncated by ZLAgent]"
    return content[: max(0, limit - len(suffix))] + suffix, True


def _bounded_value(value: Any, *, limit: int = 8_000) -> Any:
    if value is None:
        return None
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    if len(serialized) <= limit:
        return value
    return {"truncated": True, "characters": len(serialized)}
