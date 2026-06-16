"""Context models and compression pipeline exports.

该包集中保存 Agent 单轮执行所需的上下文快照、历史压缩、摘要压缩和上下文管理器。
"""

from .engine import ContextEngine, ContextEngineState, ContextEngineStats
from .state import (
    GraphEdgeSnapshot,
    GraphNodeSnapshot,
    GraphRAGSnapshot,
    MemorySnapshot,
    RuntimeSnapshot,
    TurnContext,
)
from .summary import (
    DEFAULT_FAILURE_COOLDOWN_SECONDS as SUMMARY_FAILURE_COOLDOWN_SECONDS,
    DEFAULT_KEEP_RECENT_ROUNDS as SUMMARY_KEEP_RECENT_ROUNDS,
    DEFAULT_MIN_ROUNDS_TO_COMPRESS,
    DEFAULT_SUMMARY_MAX_CHARS,
    SUMMARY_MARKER,
    SummaryCompressor,
    SummaryResult,
)
from .trajectory import (
    COMPRESSION_MARKER,
    DEFAULT_KEEP_LAST_TOOL_RESULTS,
    DEFAULT_KEEP_RECENT_ROUNDS,
    DEFAULT_MAX_CHARS,
    DEFAULT_TRUNCATED_TOOL_CHARS,
    CompressionStats,
    compress_history,
    estimate_chars,
)
from .turn_scope import current_turn_context, set_turn_context

__all__ = [
    "COMPRESSION_MARKER",
    "DEFAULT_KEEP_LAST_TOOL_RESULTS",
    "DEFAULT_KEEP_RECENT_ROUNDS",
    "DEFAULT_MAX_CHARS",
    "DEFAULT_MIN_ROUNDS_TO_COMPRESS",
    "DEFAULT_SUMMARY_MAX_CHARS",
    "DEFAULT_TRUNCATED_TOOL_CHARS",
    "GraphEdgeSnapshot",
    "GraphNodeSnapshot",
    "GraphRAGSnapshot",
    "MemorySnapshot",
    "RuntimeSnapshot",
    "SUMMARY_FAILURE_COOLDOWN_SECONDS",
    "SUMMARY_KEEP_RECENT_ROUNDS",
    "SUMMARY_MARKER",
    "TurnContext",
    "ContextEngine",
    "ContextEngineState",
    "ContextEngineStats",
    "CompressionStats",
    "SummaryCompressor",
    "SummaryResult",
    "compress_history",
    "current_turn_context",
    "estimate_chars",
    "set_turn_context",
]
