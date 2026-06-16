"""MCP (Model Context Protocol) client subsystem (v0.14).

Connects to external MCP servers via stdio, discovers their tools, and
registers each one into the ZLAgent ``ToolRegistry`` so the agent can call
them like any built-in tool.

Three big simplifications vs Hermes' ``tools/mcp_tool.py``:

* No background asyncio loop in a thread — ZLAgent is async-native, so we
  ``await`` the MCP SDK directly.
* No OAuth for env-interpolated bearer headers cover most practical
  remote servers; OAuth lands in a later milestone.
* No server-initiated sampling for the agent is the LLM caller,
  not the LLM provider.

Public exports come from the submodules; ``app.py`` only cares about
``MCPManager``.
"""

from .config import MCPServerConfig, load_mcp_config
from .manager import MCPManager
from .connection import MCPServerConnection
from .tool_wrapper import MCPTool

__all__ = [
    "MCPServerConfig",
    "load_mcp_config",
    "MCPManager",
    "MCPServerConnection",
    "MCPTool",
]
