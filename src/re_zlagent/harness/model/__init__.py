"""Model client boundaries."""

from .budget import (
    ModelCallBudget,
    TokenBudgetExceededError,
    aggregate_token_usage,
    complete_with_budget,
    estimate_message_tokens,
    estimate_text_tokens,
    normalize_token_usage,
)
from .config import OpenAICompatibleModelConfig
from .openai_compatible import (
    ChatCompletionTransport,
    ModelClientError,
    OpenAICompatibleModelClient,
    UrllibChatCompletionTransport,
)
from .types import ModelClient, ModelMessage, ModelResponse

__all__ = [
    "ChatCompletionTransport",
    "ModelCallBudget",
    "ModelClient",
    "ModelClientError",
    "ModelMessage",
    "ModelResponse",
    "OpenAICompatibleModelClient",
    "OpenAICompatibleModelConfig",
    "TokenBudgetExceededError",
    "UrllibChatCompletionTransport",
    "aggregate_token_usage",
    "complete_with_budget",
    "estimate_message_tokens",
    "estimate_text_tokens",
    "normalize_token_usage",
]
