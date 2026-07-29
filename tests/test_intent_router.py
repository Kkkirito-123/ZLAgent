from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from re_zlagent.harness.agent import (  # noqa: E402
    AgentRunRequest,
    IntentReasonCode,
    IntentRoute,
    IntentRouteError,
    JsonIntentRouter,
    parse_intent_decision,
)
from re_zlagent.harness.model import (  # noqa: E402
    ModelCallBudget,
    ModelMessage,
    ModelResponse,
    TokenBudgetExceededError,
)


class IntentRecordingModel:
    def __init__(self, response: str) -> None:
        self._response = response
        self.calls: list[tuple[ModelMessage, ...]] = []

    async def complete(
        self,
        messages: tuple[ModelMessage, ...],
    ) -> ModelResponse:
        self.calls.append(messages)
        return ModelResponse(
            content=self._response,
            raw={
                "provider": "test",
                "model": "intent-test-model",
                "usage": {"total_tokens": 21},
                "secret": "must-not-propagate",
            },
        )


class IntentRouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_routes_chat_with_filtered_provider_metadata(self) -> None:
        model = IntentRecordingModel(
            '{"route":"chat","reason_code":"direct_answer",'
            '"clarification_question":null}'
        )
        router = JsonIntentRouter(model)

        decision = await router.route(
            AgentRunRequest(
                run_id="intent-1",
                user_goal="解释一下 MCP",
                context={"surface": "test"},
            )
        )

        self.assertEqual(decision.route, IntentRoute.CHAT)
        self.assertEqual(decision.reason_code, IntentReasonCode.DIRECT_ANSWER)
        self.assertEqual(decision.metadata["provider"], "test")
        self.assertEqual(decision.metadata["usage"]["total_tokens"], 21)
        self.assertNotIn("secret", decision.metadata)
        self.assertIn('"surface": "test"', model.calls[0][1].content)

    async def test_bounds_untrusted_request_context(self) -> None:
        model = IntentRecordingModel(
            '{"route":"task","reason_code":"external_read",'
            '"clarification_question":null}'
        )
        router = JsonIntentRouter(model, max_context_chars=256)

        decision = await router.route(
            AgentRunRequest(
                run_id="intent-1",
                user_goal="读取文件",
                context={"large": "x" * 800},
            )
        )

        self.assertEqual(decision.route, IntentRoute.TASK)
        self.assertIn("[...truncated by host...]", model.calls[0][1].content)

    async def test_token_preflight_rejects_before_router_model_call(self) -> None:
        model = IntentRecordingModel(
            '{"route":"chat","reason_code":"direct_answer",'
            '"clarification_question":null}'
        )
        router = JsonIntentRouter(
            model,
            token_budget=ModelCallBudget(
                max_input_tokens=32,
                max_output_tokens=16,
            ),
        )

        with self.assertRaises(TokenBudgetExceededError):
            await router.route(
                AgentRunRequest(
                    run_id="intent-budget",
                    user_goal="x" * 2_000,
                )
            )

        self.assertEqual(model.calls, [])

    def test_parses_clarification_question(self) -> None:
        decision = parse_intent_decision(
            '{"route":"clarify","reason_code":"missing_target",'
            '"clarification_question":"你希望修改哪个文件？"}'
        )

        self.assertEqual(decision.route, IntentRoute.CLARIFY)
        self.assertEqual(decision.clarification_question, "你希望修改哪个文件？")

    def test_rejects_non_json_and_unknown_fields(self) -> None:
        with self.assertRaises(IntentRouteError):
            parse_intent_decision("task")
        with self.assertRaises(IntentRouteError):
            parse_intent_decision(
                '{"route":"chat","reason_code":"direct_answer",'
                '"clarification_question":null,"accepted":true}'
            )

    def test_rejects_reason_route_mismatch(self) -> None:
        with self.assertRaises(IntentRouteError):
            parse_intent_decision(
                '{"route":"chat","reason_code":"external_write",'
                '"clarification_question":null}'
            )

    def test_rejects_missing_clarification_question(self) -> None:
        with self.assertRaises(IntentRouteError):
            parse_intent_decision(
                '{"route":"clarify","reason_code":"missing_details",'
                '"clarification_question":null}'
            )

    def test_rejects_question_on_non_clarify_route(self) -> None:
        with self.assertRaises(IntentRouteError):
            parse_intent_decision(
                '{"route":"task","reason_code":"external_action",'
                '"clarification_question":"Should I continue?"}'
            )


if __name__ == "__main__":
    unittest.main()
