"""HTTP surface for the tool subsystem.

Endpoints:

* ``GET  /api/tools``              — list registered tools with their schema.
* ``POST /api/tools/{name}/test``  — invoke a *safe* tool directly, without
  going through the LLM. Primarily for operational debugging (e.g. verifying
  that ``web_search`` can reach DuckDuckGo from inside the container).

Confirm-tier tools are refused because this operational endpoint does not
represent interactive user approval. Attempts return ``409 Conflict``.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..harness import HarnessExecution
from ..tools import ToolPermission, ToolRegistry

router = APIRouter(prefix="/api/tools", tags=["tools"])


def _get_registry(request: Request) -> ToolRegistry:
    registry = getattr(request.app.state, "tool_registry", None)
    if registry is None:
        raise HTTPException(status_code=500, detail="tool registry not initialized")
    return registry


def _get_execution(request: Request) -> HarnessExecution:
    execution = getattr(request.app.state, "tool_execution", None)
    if execution is None:
        raise HTTPException(status_code=500, detail="tool execution boundary not initialized")
    return execution


class ToolDescriptor(BaseModel):
    name: str
    description: str
    permission: str
    parameters_schema: dict[str, Any]
    is_read_only: bool
    is_concurrency_safe: bool
    is_destructive: bool
    should_defer: bool
    always_load: bool
    search_hint: str


class ToolListResponse(BaseModel):
    count: int
    counts_by_permission: dict[str, int]
    tools: list[ToolDescriptor]


class ToolTestPayload(BaseModel):
    arguments: dict[str, Any] = Field(
        default_factory=dict,
        description="JSON object passed to the tool as its arguments dict.",
    )


class ToolTestResponse(BaseModel):
    ok: bool
    content: str
    error: Optional[str] = None
    status: str
    error_type: Optional[str] = None
    recoverable_by_model: bool
    recommended_next_action: Optional[str] = None
    source: str
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    side_effects: list[dict[str, Any]] = Field(default_factory=list)


@router.get("", response_model=ToolListResponse)
async def list_tools(request: Request) -> ToolListResponse:
    registry = _get_registry(request)
    descriptors = [
        ToolDescriptor(
            name=tool.name,
            description=tool.description,
            permission=tool.permission.value,
            parameters_schema=tool.parameters_schema,
            is_read_only=tool.is_read_only,
            is_concurrency_safe=tool.is_concurrency_safe,
            is_destructive=tool.is_destructive,
            should_defer=tool.should_defer,
            always_load=tool.always_load,
            search_hint=tool.search_hint,
        )
        for tool in registry.list()
    ]
    return ToolListResponse(
        count=len(registry),
        counts_by_permission=registry.counts_by_permission(),
        tools=descriptors,
    )


@router.post("/{name}/test", response_model=ToolTestResponse)
async def test_tool(name: str, payload: ToolTestPayload, request: Request) -> ToolTestResponse:
    registry = _get_registry(request)
    tool = registry.get(name)
    if tool is None:
        raise HTTPException(status_code=404, detail=f"no such tool: {name}")
    if tool.permission is ToolPermission.CONFIRM:
        raise HTTPException(
            status_code=409,
            detail=(
                f"tool '{name}' is permission=confirm; direct testing requires the"
                " interactive confirmation flow"
            ),
        )
    if tool.permission is ToolPermission.DENY:  # defensive; registry drops these
        raise HTTPException(status_code=409, detail=f"tool '{name}' is deny-listed")
    result = await _get_execution(request).execute(
        name,
        payload.arguments,
        session_id="api:tool-test",
    )
    metadata = result.to_metadata()
    return ToolTestResponse(
        ok=result.ok,
        content=result.content,
        error=result.error,
        **{key: value for key, value in metadata.items() if key != "ok"},
    )
