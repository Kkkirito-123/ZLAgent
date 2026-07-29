from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from re_zlagent.harness.model import (  # noqa: E402
    ModelCallBudget,
    ModelMessage,
    ModelResponse,
    TokenBudgetExceededError,
    aggregate_token_usage,
    complete_with_budget,
    estimate_message_tokens,
    estimate_text_tokens,
)


class PlainBudgetModel:
    def __init__(self, response: ModelResponse) -> None:
        self.response = response
        self.calls = 0

    async def complete(
        self,
        messages: tuple[ModelMessage, ...],
    ) -> ModelResponse:
        self.calls += 1
        return self.response


class ConfigurableBudgetModel(PlainBudgetModel):
    def __init__(self, response: ModelResponse) -> None:
        super().__init__(response)
        self.max_tokens: list[int | None] = []

    async def complete_with_options(
        self,
        messages: tuple[ModelMessage, ...],
        *,
        max_tokens: int | None = None,
    ) -> ModelResponse:
        self.calls += 1
        self.max_tokens.append(max_tokens)
        return self.response


class ModelBudgetTests(unittest.IsolatedAsyncioTestCase):
    def test_estimator_is_compact_for_ascii_and_conservative_for_chinese(self) -> None:
        self.assertEqual(estimate_text_tokens("abcdefgh"), 2)
        self.assertEqual(estimate_text_tokens("你好"), 2)
        self.assertGreater(
            estimate_message_tokens((ModelMessage(role="user", content="hi"),)),
            2,
        )

    async def test_preflight_rejects_without_calling_provider(self) -> None:
        model = PlainBudgetModel(ModelResponse(content="unused"))

        with self.assertRaises(TokenBudgetExceededError):
            await complete_with_budget(
                model,
                (ModelMessage(role="user", content="x" * 200),),
                budget=ModelCallBudget(
                    max_input_tokens=10,
                    max_output_tokens=10,
                ),
            )

        self.assertEqual(model.calls, 0)

    async def test_provider_cap_and_budget_metadata_are_observable(self) -> None:
        model = ConfigurableBudgetModel(
            ModelResponse(
                content="ok",
                raw={
                    "usage": {
                        "prompt_tokens": 12,
                        "completion_tokens": 3,
                        "total_tokens": 15,
                    }
                },
            )
        )
        response = await complete_with_budget(
            model,
            (ModelMessage(role="user", content="hello"),),
            budget=ModelCallBudget(
                max_input_tokens=100,
                max_output_tokens=20,
            ),
        )

        self.assertEqual(model.max_tokens, [20])
        self.assertTrue(response.raw["token_budget"]["output_limit_enforced"])
        self.assertEqual(
            response.raw["token_budget"]["usage"]["total_tokens"],
            15,
        )

    async def test_provider_overage_is_rejected_after_observation(self) -> None:
        model = PlainBudgetModel(
            ModelResponse(
                content="too much",
                raw={
                    "usage": {
                        "prompt_tokens": 5,
                        "completion_tokens": 30,
                        "total_tokens": 35,
                    }
                },
            )
        )

        with self.assertRaises(TokenBudgetExceededError):
            await complete_with_budget(
                model,
                (ModelMessage(role="user", content="hello"),),
                budget=ModelCallBudget(
                    max_input_tokens=100,
                    max_output_tokens=10,
                ),
            )

    def test_usage_aggregation_normalizes_common_provider_names(self) -> None:
        usage = aggregate_token_usage(
            {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            {"input_tokens": 4, "output_tokens": 1, "total_tokens": 5},
        )

        self.assertEqual(
            usage,
            {"input_tokens": 14, "output_tokens": 3, "total_tokens": 17},
        )


if __name__ == "__main__":
    unittest.main()
