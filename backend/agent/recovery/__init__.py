"""Recovery, correction and guardrail exports.

该包集中保存用户纠正检测、工具循环保护和失败模式学习等错误恢复能力。
"""

from .correction import (
    CorrectionConfidence,
    CorrectionSignal,
    detect_correction,
    render_review_hint,
)
from .failure_learning import (
    DEFAULT_FAILURE_THRESHOLD,
    FailureLearner,
    FailureRecord,
    fingerprint_error,
)
from .tool_guardrails import (
    ToolCallGuardrailConfig,
    ToolCallGuardrailController,
    ToolCallSignature,
    ToolGuardrailDecision,
    append_toolguard_guidance,
    canonical_tool_args,
    toolguard_synthetic_result,
)

__all__ = [
    "CorrectionConfidence",
    "CorrectionSignal",
    "DEFAULT_FAILURE_THRESHOLD",
    "FailureLearner",
    "FailureRecord",
    "ToolCallGuardrailConfig",
    "ToolCallGuardrailController",
    "ToolCallSignature",
    "ToolGuardrailDecision",
    "append_toolguard_guidance",
    "canonical_tool_args",
    "detect_correction",
    "fingerprint_error",
    "render_review_hint",
    "toolguard_synthetic_result",
]

