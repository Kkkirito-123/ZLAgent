from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from re_zlagent.harness.model import (  # noqa: E402
    ModelClientError,
    ModelMessage,
    OpenAICompatibleModelClient,
)


class FakeTransport:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self,
        *,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        self.calls.append({
            "url": url,
            "headers": dict(headers),
            "payload": dict(payload),
            "timeout_seconds": timeout_seconds,
        })
        return self.response


class OpenAICompatibleModelClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_posts_chat_completion_payload(self) -> None:
        transport = FakeTransport({
            "choices": [
                {
                    "message": {"content": "{\"ok\": true}"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10},
        })
        client = OpenAICompatibleModelClient(
            base_url="https://provider.example/v1/",
            model="planner-model",
            api_key="secret",
            temperature=0,
            max_tokens=512,
            transport=transport,
        )

        response = await client.complete((
            ModelMessage(role="system", content="plan"),
            ModelMessage(role="user", content="goal"),
        ))

        self.assertEqual(response.content, "{\"ok\": true}")
        call = transport.calls[0]
        self.assertEqual(call["url"], "https://provider.example/v1/chat/completions")
        self.assertEqual(call["headers"]["Authorization"], "Bearer secret")
        self.assertEqual(call["payload"]["model"], "planner-model")
        self.assertEqual(call["payload"]["temperature"], 0)
        self.assertEqual(call["payload"]["max_tokens"], 512)
        self.assertEqual(call["payload"]["messages"][1]["content"], "goal")
        self.assertEqual(response.raw["finish_reason"], "stop")
        self.assertEqual(response.raw["usage"], {"prompt_tokens": 10})

    async def test_api_key_is_optional_for_local_compatible_servers(self) -> None:
        transport = FakeTransport({
            "choices": [{"message": {"content": "ok"}}],
        })
        client = OpenAICompatibleModelClient(
            base_url="http://localhost:8000/v1",
            model="local",
            transport=transport,
        )

        await client.complete((ModelMessage(role="user", content="hi"),))

        self.assertNotIn("Authorization", transport.calls[0]["headers"])

    async def test_content_parts_are_joined_when_provider_returns_list(self) -> None:
        transport = FakeTransport({
            "choices": [
                {
                    "message": {
                        "content": [
                            {"type": "text", "text": "hello"},
                            {"type": "text", "text": " world"},
                        ]
                    }
                }
            ],
        })
        client = OpenAICompatibleModelClient(
            base_url="https://provider.example/v1",
            model="planner-model",
            transport=transport,
        )

        response = await client.complete((ModelMessage(role="user", content="hi"),))

        self.assertEqual(response.content, "hello world")

    async def test_invalid_provider_response_is_error_data(self) -> None:
        transport = FakeTransport({"choices": []})
        client = OpenAICompatibleModelClient(
            base_url="https://provider.example/v1",
            model="planner-model",
            transport=transport,
        )

        with self.assertRaises(ModelClientError):
            await client.complete((ModelMessage(role="user", content="hi"),))

    def test_client_configuration_is_validated(self) -> None:
        with self.assertRaises(ValueError):
            OpenAICompatibleModelClient(base_url="", model="model")
        with self.assertRaises(ValueError):
            OpenAICompatibleModelClient(base_url="https://provider.example/v1", model="")
        with self.assertRaises(ValueError):
            OpenAICompatibleModelClient(
                base_url="https://provider.example/v1",
                model="model",
                timeout_seconds=0,
            )


if __name__ == "__main__":
    unittest.main()
