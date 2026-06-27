"""Tool subsystem for ZLAgent.

A *tool* is a callable unit the LLM can invoke through the OpenAI tools /
function-calling protocol to perform actions beyond text generation (fetch a
URL, search the web, read a workspace file, ...). Each tool exposes a JSON
schema for its parameters, a permission tier, and an async ``execute()``.

The registry is the single place tools are discovered and filtered. The agent
loop consumes the registry in :mod:`backend.agent.loop`.
"""
from .base import Tool, ToolPermission, ToolResult
from .permission import PermissionDecision, PermissionPolicy, PermissionResult
from .registry import ToolRegistry

__all__ = [
    "PermissionDecision",
    "PermissionPolicy",
    "PermissionResult",
    "Tool",
    "ToolPermission",
    "ToolResult",
    "ToolRegistry",
]
