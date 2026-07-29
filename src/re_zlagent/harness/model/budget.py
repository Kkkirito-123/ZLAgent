"""Small per-call token budgets for model-facing harness boundaries."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from math import ceil
from typing import Any, cast

from .types import ModelClient, ModelMessage, ModelResponse


class TokenBudgetExceededError(RuntimeError):
    """Raised before unsafe work continues after a model budget is exceeded."""


@dataclass(frozen=True, slots=True)
class ModelCallBudget:
    """One bounded model call.

    Input estimation is deliberately conservative for non-ASCII text. The
    provider-reported usage remains the authoritative observation after a call.
    """

    max_input_tokens: int
    max_output_tokens: int
    max_total_tokens: int | None = None

    def __post_init__(self) -> None:
        if self.max_input_tokens <= 0:
            raise ValueError("max_input_tokens must be positive")
        if self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        total = self.max_total_tokens
        if total is None:
            total = self.max_input_tokens + self.max_output_tokens
            object.__setattr__(self, "max_total_tokens", total)
        if total <= 0:
            raise ValueError("max_total_tokens must be positive")

    def to_dict(self) -> dict[str, int]:
        assert self.max_total_tokens is not None
        return {
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "max_total_tokens": self.max_total_tokens,
        }


def estimate_text_tokens(value: str) -> int:
    """Estimate tokens without binding the harness to one tokenizer."""

    ascii_chars = sum(ord(char) < 128 for char in value)
    non_ascii_chars = len(value) - ascii_chars
    return ceil(ascii_chars / 4) + non_ascii_chars


def estimate_message_tokens(messages: tuple[ModelMessage, ...]) -> int:
    """Conservatively estimate chat-message input including small framing cost."""

    if not messages:
        return 0
    return 3 + sum(
        4 + estimate_text_tokens(message.role) + estimate_text_tokens(message.content)
        for message in messages
    )


def normalize_token_usage(value: Any) -> dict[str, int]:
    """Normalize common provider usage names without retaining provider payloads."""

    if not isinstance(value, dict):
        return {}
    input_tokens = _first_int(value, "prompt_tokens", "input_tokens", "input")
    output_tokens = _first_int(
        value,
        "completion_tokens",
        "output_tokens",
        "output",
    )
    total_tokens = _first_int(value, "total_tokens")
    if total_tokens is None and (input_tokens is not None or output_tokens is not None):
        total_tokens = (input_tokens or 0) + (output_tokens or 0)
    normalized: dict[str, int] = {}
    if input_tokens is not None:
        normalized["input_tokens"] = input_tokens
    if output_tokens is not None:
        normalized["output_tokens"] = output_tokens
    if total_tokens is not None:
        normalized["total_tokens"] = total_tokens
    return normalized


def aggregate_token_usage(*values: Any) -> dict[str, int]:
    """Sum normalized model usage across observable request phases."""

    total = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    observed = False
    for value in values:
        usage = normalize_token_usage(value)
        if not usage:
            continue
        observed = True
        for key in total:
            total[key] += usage.get(key, 0)
    return total if observed else {}


async def complete_with_budget(
    model: ModelClient,
    messages: tuple[ModelMessage, ...],
    *,
    budget: ModelCallBudget,
    response_format: str | None = None,
) -> ModelResponse:
    """Call a model with preflight, provider cap, and post-call usage checks."""

    estimated_input = estimate_message_tokens(messages)
    if estimated_input > budget.max_input_tokens:
        raise TokenBudgetExceededError(
            "estimated model input exceeds token budget: "
            f"{estimated_input} > {budget.max_input_tokens}"
        )
    assert budget.max_total_tokens is not None
    maximum_output = min(
        budget.max_output_tokens,
        budget.max_total_tokens - estimated_input,
    )
    if maximum_output <= 0:
        raise TokenBudgetExceededError(
            "model token budget leaves no room for an output"
        )

    configurable_complete = getattr(model, "complete_with_options", None)
    output_limit_enforced = callable(configurable_complete)
    if output_limit_enforced:
        complete_with_options = cast(
            Callable[..., Awaitable[ModelResponse]],
            configurable_complete,
        )
        options: dict[str, Any] = {"max_tokens": maximum_output}
        if response_format is not None:
            options["response_format"] = response_format
        response = await complete_with_options(
            messages,
            **options,
        )
    else:
        response = await model.complete(messages)

    usage = normalize_token_usage(response.raw.get("usage"))
    observed_input = usage.get("input_tokens")
    observed_output = usage.get("output_tokens")
    observed_total = usage.get("total_tokens")
    if observed_input is not None and observed_input > budget.max_input_tokens:
        raise TokenBudgetExceededError(
            "provider-reported input exceeds token budget: "
            f"{observed_input} > {budget.max_input_tokens}"
        )
    if observed_output is not None and observed_output > maximum_output:
        raise TokenBudgetExceededError(
            "provider-reported output exceeds token budget: "
            f"{observed_output} > {maximum_output}"
        )
    if observed_total is not None and observed_total > budget.max_total_tokens:
        raise TokenBudgetExceededError(
            "provider-reported total exceeds token budget: "
            f"{observed_total} > {budget.max_total_tokens}"
        )
    estimated_output = estimate_text_tokens(response.content)
    if observed_output is None and estimated_output > maximum_output:
        raise TokenBudgetExceededError(
            "estimated model output exceeds token budget: "
            f"{estimated_output} > {maximum_output}"
        )

    raw = dict(response.raw)
    raw["token_budget"] = {
        **budget.to_dict(),
        "estimated_input_tokens": estimated_input,
        "effective_max_output_tokens": maximum_output,
        "estimated_output_tokens": estimated_output,
        "output_limit_enforced": output_limit_enforced,
        "response_format": response_format,
        "usage": usage,
    }
    return ModelResponse(content=response.content, raw=raw)


def _first_int(value: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        candidate = value.get(key)
        if (
            isinstance(candidate, int)
            and not isinstance(candidate, bool)
            and candidate >= 0
        ):
            return candidate
    return None
