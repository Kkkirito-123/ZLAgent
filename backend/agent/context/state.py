"""Agent turn and runtime snapshot models.

该模块保存 Agent 单轮执行期间共享的轻量状态模型。模型只描述数据形状，
具体加载和写入逻辑放在 `turn_preparer`、`memory` 和 `harness` 分组中。
"""

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(slots=True)
class MemorySnapshot:
    """Lightweight view of memory state available to the agent."""

    control_axioms: list[Any] = field(default_factory=list)
    agent_notes: list[Any] = field(default_factory=list)
    user_facts: list[Any] = field(default_factory=list)
    provider_blocks: list[Any] = field(default_factory=list)
    rendered_block: str = ""


@dataclass(slots=True)
class GraphNodeSnapshot:
    """Single node within a GraphRAG-ready knowledge graph."""

    id: str
    label: str
    kind: str
    knowledge_base_id: str = "default"
    attrs: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class GraphEdgeSnapshot:
    """Directed edge connecting two graph nodes."""

    source: str
    target: str
    kind: str
    attrs: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class GraphRAGSnapshot:
    """Compact view of the GraphRAG state exposed to the agent and UI."""

    current_knowledge_base_id: str = "default"
    knowledge_base_count: int = 0
    node_count: int = 0
    edge_count: int = 0
    nodes: list[GraphNodeSnapshot] = field(default_factory=list)
    edges: list[GraphEdgeSnapshot] = field(default_factory=list)
    selected_node_id: Optional[str] = None
    summary: str = ""


@dataclass(slots=True)
class RuntimeSnapshot:
    """Lightweight view of runtime state available to the agent."""

    llm_configured: bool = False
    tool_count: int = 0
    core_status: dict[str, Any] = field(default_factory=dict)
    service_status: dict[str, Any] = field(default_factory=dict)
    extension_status: dict[str, Any] = field(default_factory=dict)
    graph_rag: Optional[GraphRAGSnapshot] = None


@dataclass(slots=True)
class TurnContext:
    """Per-turn context shared with tools and agent collaborators."""

    turn_id: str = ""
    session_id: str = ""
    platform: str = ""
    user_id: str = ""
    reply_target: Any = None
    is_interactive: bool = True
    user_message: str = ""
    memory: Optional[MemorySnapshot] = None
    runtime: Optional[RuntimeSnapshot] = None


__all__ = [
    "MemorySnapshot",
    "GraphNodeSnapshot",
    "GraphEdgeSnapshot",
    "GraphRAGSnapshot",
    "RuntimeSnapshot",
    "TurnContext",
]
